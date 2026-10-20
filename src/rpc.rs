//! Genuine loopback HTTP JSON-RPC transport. No synthetic wallet responses.
use crate::{
    engine::Node,
    intent::{digest, Intent},
    Error, Result,
};
use base64::Engine as _;
use serde_json::{json, Value};
use std::{net::IpAddr, time::Duration};

#[derive(Clone)]
pub struct Rpc {
    url: String,
    authorization: Option<String>,
    agent: ureq::Agent,
}
impl Rpc {
    pub fn new(url: &str, credential: Option<&str>) -> Result<Self> {
        Self::with_timeout(url, credential, Duration::from_secs(30))
    }
    pub fn with_timeout(url: &str, credential: Option<&str>, timeout: Duration) -> Result<Self> {
        if timeout.is_zero() || timeout > Duration::from_secs(900) {
            return Err(Error::Invalid(
                "RPC timeout must be positive and at most 900 seconds".into(),
            ));
        }
        // Avoid DNS, proxies, redirects, production endpoints, and accidental public exposure.
        let endpoint = url
            .strip_prefix("http://")
            .ok_or_else(|| Error::Invalid("local http:// RPC URL required".into()))?;
        let authority = endpoint.strip_suffix('/').unwrap_or(endpoint);
        let addr: std::net::SocketAddr = authority
            .parse()
            .map_err(|_| Error::Invalid("RPC must be a numeric loopback IP:port".into()))?;
        if !addr.ip().is_loopback() {
            return Err(Error::Invalid("RPC must use loopback".into()));
        }
        let authorization = credential.map(|c| {
            format!(
                "Basic {}",
                base64::engine::general_purpose::STANDARD.encode(c.trim())
            )
        });
        let agent = ureq::AgentBuilder::new()
            .timeout(timeout)
            .redirects(0)
            .try_proxy_from_env(false)
            .build();
        Ok(Self {
            url: format!("http://{addr}/"),
            authorization,
            agent,
        })
    }
    pub fn call(&self, method: &str, params: Value) -> Result<Value> {
        let id = uuid::Uuid::new_v4().to_string();
        let mut request = self.agent.post(&self.url);
        if let Some(auth) = &self.authorization {
            request = request.set("Authorization", auth);
        }
        let response =
            request.send_json(json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}));
        let response = match response {
            Ok(r) | Err(ureq::Error::Status(_, r)) => r,
            Err(_) => {
                return Err(Error::Remote(format!(
                    "{method}: transport failure; outcome unknown"
                )))
            }
        };
        let status = response.status();
        let mut reader = response.into_reader().take(16 * 1024 * 1024 + 1);
        let mut body = Vec::new();
        use std::io::Read;
        reader.read_to_end(&mut body)?;
        if body.len() > 16 * 1024 * 1024 {
            return Err(Error::Remote("RPC response exceeds limit".into()));
        }
        let value: Value = serde_json::from_slice(&body)
            .map_err(|_| Error::Remote(format!("{method}: invalid JSON; outcome unknown")))?;
        if value.get("id") != Some(&json!(id)) {
            return Err(Error::Remote(
                "RPC response ID mismatch; outcome unknown".into(),
            ));
        }
        if !value.get("error").unwrap_or(&Value::Null).is_null() {
            // Persist method/code only; remote text may contain credentials or wallet secrets.
            return Err(Error::Remote(format!(
                "{method}: JSON-RPC error code {}; outcome requires classification",
                value["error"]["code"]
            )));
        }
        if !(200..300).contains(&status) {
            return Err(Error::Remote(format!(
                "{method}: HTTP {status}; outcome unknown"
            )));
        }
        value
            .get("result")
            .cloned()
            .ok_or_else(|| Error::Remote("RPC result missing".into()))
    }
    pub fn probe(&self) -> Result<Value> {
        self.call("getinfo", json!([]))
    }
}

pub struct Zebra {
    pub rpc: Rpc,
}

fn submission_receipt(value: &Value, expected_txid: &str) -> Result<String> {
    if expected_txid.len() == 64
        && hex::decode(expected_txid).is_ok()
        && value
            .as_str()
            .is_some_and(|observed| observed.eq_ignore_ascii_case(expected_txid))
    {
        // Legacy committed rows may use uppercase hex. Preserve their recorded
        // identity while accepting Zebra's canonical spelling of the same ID.
        Ok(expected_txid.into())
    } else {
        Err(Error::Remote("Zebra returned an unexpected txid".into()))
    }
}

pub fn pinned_regtest(info: &Value, genesis: &Value) -> bool {
    let upgrades = [
        ("5ba81b19", 1),
        ("76b809bb", 1),
        ("2bb40e60", 1),
        ("f5b9230b", 1),
        ("e9ff75a6", 1),
        ("c2d6d0b4", 2),
        ("c8e71055", 2),
        ("4dec4df0", 2),
        ("5437f330", 2),
        ("37a5165b", 2),
    ];
    // Zebra's BIP70 label is "test" for both regtest and public testnet.
    // Require the distinct genesis and the exact qualified upgrade schedule.
    info["chain"] == "test"
        && genesis == "029f11d80ef9765602235e1bc9727e3eb6ba20839319f761fee920d63401e327"
        && info["upgrades"]
            .as_object()
            .is_some_and(|u| u.len() == upgrades.len())
        && upgrades.iter().all(|(branch, height)| {
            info["upgrades"][branch]["activationheight"] == *height
                && info["upgrades"][branch]["status"] == "active"
        })
        && info["consensus"]["nextblock"] == "37a5165b"
}
impl Node for Zebra {
    fn preflight(&self, intent: &Intent, _: &str, raw: &[u8]) -> Result<()> {
        let info = self.rpc.call("getblockchaininfo", json!([]))?;
        let genesis = self.rpc.call("getblockhash", json!([0]))?;
        if !pinned_regtest(&info, &genesis) {
            return Err(Error::Reconciliation(
                "Zebra is outside pinned NU6.3 regtest profile".into(),
            ));
        }
        let height = info["blocks"]
            .as_u64()
            .ok_or_else(|| Error::Remote("missing chain height".into()))?;
        let expiry = verified_v6_expiry(raw, intent)?;
        if height + 1 >= u64::from(expiry) {
            return Err(Error::Reconciliation(
                "approved expiry may have been reached; never replace automatically".into(),
            ));
        }
        Ok(())
    }
    fn submit(&self, raw: &[u8], txid: &str) -> Result<String> {
        match self
            .rpc
            .call("sendrawtransaction", json!([hex::encode(raw)]))
        {
            Ok(v) => submission_receipt(&v, txid),
            Err(error) => {
                // Duplicate acceptance must be observed, never guessed from an error string.
                if let Ok(v) = self.rpc.call("getrawtransaction", json!([txid, 0])) {
                    if let Some(hex) = v.as_str() {
                        if let Ok(observed) = hex::decode(hex) {
                            if digest(&observed) == digest(raw) {
                                return Ok(txid.into());
                            }
                        }
                    }
                }
                Err(error)
            }
        }
    }
    fn observe(&self, txid: &str, depth: u32) -> Result<Value> {
        let info = self.rpc.call("getblockchaininfo", json!([]))?;
        let genesis = self.rpc.call("getblockhash", json!([0]))?;
        if !pinned_regtest(&info, &genesis) {
            return Err(Error::Reconciliation("chain profile changed".into()));
        }
        // A failed lookup is uncertainty, never proof of rejection or permission
        // to construct another payout. Record it without changing committed bytes.
        match self.rpc.call("getrawtransaction", json!([txid, 1])) {
            Ok(tx) => {
                let count = tx["confirmations"].as_u64().unwrap_or(0);
                let state = if count >= u64::from(depth) {
                    "confirmed"
                } else if count > 0 {
                    "mined"
                } else {
                    "node_observed"
                };
                Ok(
                    json!({"state":state,"confirmations":count,"required_confirmations":depth,"blockhash":tx.get("blockhash"),"tip_height":info["blocks"],"txid":txid}),
                )
            }
            Err(_) => Ok(
                json!({"state":"unknown","confirmations":0,"required_confirmations":depth,"blockhash":null,"tip_height":info["blocks"],"txid":txid}),
            ),
        }
    }
}

/// Read the fixed header of bytes already independently parsed/verified before
/// commitment. This does not replace the adapter's full consensus verification.
pub fn verified_v6_expiry(raw: &[u8], intent: &Intent) -> Result<u32> {
    let header = raw.get(..20).ok_or(Error::Integrity)?;
    let word = |offset| {
        u32::from_le_bytes(
            header[offset..offset + 4]
                .try_into()
                .expect("bounded header"),
        )
    };
    if word(0) != 0x8000_0006 || word(4) != 0xd884_b698 || word(8) != 0x37a5_165b || word(12) != 0 {
        return Err(Error::Integrity);
    }
    let expiry = word(16);
    if expiry < intent.expiry.min_height || expiry > intent.expiry.max_height {
        return Err(Error::Integrity);
    }
    Ok(expiry)
}

pub fn loopback_bind(value: &str) -> Result<std::net::SocketAddr> {
    let addr: std::net::SocketAddr = value
        .parse()
        .map_err(|_| Error::Invalid("numeric listen IP:port required".into()))?;
    if !matches!(addr.ip(),IpAddr::V4(ip) if ip.is_loopback())
        && !matches!(addr.ip(),IpAddr::V6(ip) if ip.is_loopback())
    {
        return Err(Error::Invalid("HTTP API must listen on loopback".into()));
    }
    Ok(addr)
}

#[cfg(test)]
mod pinned_profile_tests {
    use super::{pinned_regtest, submission_receipt};
    use serde_json::{json, Value};

    const REGTEST_GENESIS: &str =
        "029f11d80ef9765602235e1bc9727e3eb6ba20839319f761fee920d63401e327";
    // Official selected Zebra source: parameters/checkpoint/list.rs and
    // parameters/network/testnet.rs. Public testnet shares BIP70 name "test".
    const MAINNET_GENESIS: &str =
        "00040fe8ec8471911baa1db1266ea15dd06b4a8a5c453883c000b031973dce08";
    const PUBLIC_TESTNET_GENESIS: &str =
        "05a60a92d99d85997cce3b87616c089f6124d7342af37106edc76126334a2c38";

    #[test]
    fn canonical_node_receipt_preserves_valid_legacy_txid_spelling() {
        let legacy = "AB".repeat(32);
        assert_eq!(
            submission_receipt(&json!(legacy.to_ascii_lowercase()), &legacy).unwrap(),
            legacy
        );
        assert!(submission_receipt(&json!("ac".repeat(32)), &legacy).is_err());
        assert!(submission_receipt(&json!(null), &legacy).is_err());
        for invalid in ["ab".repeat(31), "gg".repeat(32)] {
            assert!(submission_receipt(&json!(invalid), &invalid).is_err());
        }
    }

    fn actual_node_report() -> Value {
        let report: Value = serde_json::from_str(include_str!(
            "../integration/evidence/runtime-node-handoff.json"
        ))
        .expect("captured genuine Zebra JSON-RPC response");
        report["result"].clone()
    }

    #[test]
    fn accepts_captured_real_nu63_regtest_report() {
        let info = actual_node_report();
        assert_eq!(info["chain"], "test");
        assert!(pinned_regtest(&info, &json!(REGTEST_GENESIS)));
    }

    #[test]
    fn rejects_mainnet_and_public_testnet_geneses() {
        let info = actual_node_report();
        for genesis in [MAINNET_GENESIS, PUBLIC_TESTNET_GENESIS] {
            // Retain the superficially matching "test" name and upgrade data:
            // the real public-network genesis must independently reject it.
            assert!(!pinned_regtest(&info, &json!(genesis)), "{genesis}");
        }
        let mut mainnet_label = info;
        mainnet_label["chain"] = json!("main");
        assert!(!pinned_regtest(&mainnet_label, &json!(REGTEST_GENESIS)));
    }

    #[test]
    fn rejects_altered_upgrade_activation() {
        let mut info = actual_node_report();
        info["upgrades"]["37a5165b"]["activationheight"] = json!(3);
        assert!(!pinned_regtest(&info, &json!(REGTEST_GENESIS)));
    }

    #[test]
    fn rejects_missing_or_additional_upgrade() {
        let mut missing = actual_node_report();
        missing["upgrades"]
            .as_object_mut()
            .unwrap()
            .remove("5437f330");
        assert!(!pinned_regtest(&missing, &json!(REGTEST_GENESIS)));
        let mut additional = actual_node_report();
        additional["upgrades"]["ffffffff"] =
            json!({"name":"unqualified future upgrade", "activationheight":3, "status":"pending"});
        assert!(!pinned_regtest(&additional, &json!(REGTEST_GENESIS)));
    }

    #[test]
    fn rejects_inactive_upgrade_or_different_next_branch() {
        let mut pending = actual_node_report();
        pending["upgrades"]["37a5165b"]["status"] = json!("pending");
        assert!(!pinned_regtest(&pending, &json!(REGTEST_GENESIS)));
        let mut next = actual_node_report();
        next["consensus"]["nextblock"] = json!("5437f330");
        assert!(!pinned_regtest(&next, &json!(REGTEST_GENESIS)));
    }
}
