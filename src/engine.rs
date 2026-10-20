use crate::{
    intent::Intent,
    store::{OperationBinding, Payment, Store},
    Error, Result,
};
use std::sync::Arc;

/// Implementations must establish the full version-specific contract before returning Ok.
/// A no-broadcast source observation alone is not sufficient qualification.
pub trait Wallet: Send + Sync {
    fn qualified(&self) -> Result<()>;
    fn create(&self, intent: &Intent, binding: &OperationBinding) -> Result<Vec<u8>>;
    fn prove(&self, pczt: &[u8]) -> Result<Vec<u8>>;
    fn sign(&self, pczt: &[u8], intent: &Intent) -> Result<Vec<u8>>;
    /// Verify recipients, values, exact memo semantics, fee, expiry, branch,
    /// shielded-only inputs/outputs, and all change against wallet keys.
    fn inspect(&self, pczt: &[u8], intent: &Intent) -> Result<()>;
    /// Parse and independently verify final bytes and derive the Zcash consensus txid.
    fn extract_verified(&self, pczt: &[u8], intent: &Intent) -> Result<(Vec<u8>, String)>;
    fn recover_created(
        &self,
        _: &std::path::Path,
        _: &Intent,
        _: &OperationBinding,
    ) -> Result<Vec<u8>> {
        Err(Error::Unqualified(
            "saved CREATE response recovery is unavailable".into(),
        ))
    }
}

pub trait Node: Send + Sync {
    fn preflight(&self, intent: &Intent, txid: &str, raw: &[u8]) -> Result<()>;
    fn submit(&self, raw: &[u8], expected_txid: &str) -> Result<String>;
    fn observe(&self, _: &str, _: u32) -> Result<serde_json::Value> {
        Err(Error::Unqualified(
            "chain observation is unavailable".into(),
        ))
    }
}

/// Default executable publication gate, including pre-existing committed records.
/// The wallet gate alone cannot protect restored states because postcommit retries
/// deliberately bypass every wallet call. The explicit disposable live profile
/// uses the independently qualified Zallet/Zebra adapters instead.
pub struct PublicationDisabled;
impl Node for PublicationDisabled {
    fn preflight(&self, _: &Intent, _: &str, _: &[u8]) -> Result<()> {
        Err(Error::Unqualified(
            "publication is disabled for this qualification build, including restored committed transactions".into(),
        ))
    }
    fn submit(&self, _: &[u8], _: &str) -> Result<String> {
        Err(Error::Unqualified("publication is disabled".into()))
    }
}

pub struct Engine {
    pub store: Store,
    wallet: Arc<dyn Wallet>,
    node: Arc<dyn Node>,
}

impl Engine {
    pub fn new(store: Store, wallet: Arc<dyn Wallet>, node: Arc<dyn Node>) -> Self {
        Self {
            store,
            wallet,
            node,
        }
    }

    /// Idempotent registration never implicitly retries an uncertain operation.
    pub fn submit_or_return(&self, id: &str, intent: Intent) -> Result<Payment> {
        self.store.submit(id, intent)
    }

    pub fn retry(&self, id: &str) -> Result<Payment> {
        let mut p = self.store.get(id)?;
        if p.stage == "accepted" {
            return Ok(p);
        }
        // Branch before touching the wallet. Every postcommit retry bypasses construction.
        if p.txid.is_some() {
            return self.publish(id);
        }
        if let Some(op) = &p.inflight {
            return Err(Error::Reconciliation(op.clone()));
        }
        self.wallet.qualified()?;
        // Resume a completed artifact under its SAME persistent owner. The next
        // stage/inflight CAS still admits only one remote operation. Any uncertain
        // operation above remains fenced; elapsed time never changes ownership.
        let owner = p
            .owner
            .clone()
            .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        loop {
            let result = match p.stage.as_str() {
                "pending" => {
                    self.store.begin_stage(id, "pending", "create", &owner)?;
                    let binding = self.store.create_binding(id)?;
                    self.wallet.create(&p.intent, &binding).and_then(|pczt| {
                        self.store
                            .finish_stage(id, "create", "created", &owner, &pczt)
                    })
                }
                "created" => {
                    let pczt = self.store.artifact(id, "created")?;
                    self.wallet.inspect(&pczt, &p.intent)?;
                    self.store.begin_stage(id, "created", "prove", &owner)?;
                    self.wallet.prove(&pczt).and_then(|pczt| {
                        self.store
                            .finish_stage(id, "prove", "proven", &owner, &pczt)
                    })
                }
                "proven" => {
                    let pczt = self.store.artifact(id, "proven")?;
                    self.wallet.inspect(&pczt, &p.intent)?;
                    self.store.begin_stage(id, "proven", "sign", &owner)?;
                    self.wallet.sign(&pczt, &p.intent).and_then(|pczt| {
                        self.store.finish_stage(id, "sign", "signed", &owner, &pczt)
                    })
                }
                "signed" => {
                    let pczt = self.store.artifact(id, "signed")?;
                    self.wallet.inspect(&pczt, &p.intent)?;
                    self.store.begin_stage(id, "signed", "extract", &owner)?;
                    self.wallet
                        .extract_verified(&pczt, &p.intent)
                        .and_then(|(raw, txid)| self.store.commit_final(id, &owner, &raw, &txid))
                }
                _ => return Err(Error::Reconciliation("unexpected precommit stage".into())),
            };
            if let Err(error) = result {
                // Never clear a remote-operation fence on error, timeout, or failed output COMMIT.
                // Failure to save diagnostic text also must not clear the original durable marker.
                let _ = self.store.note_error(id, None, &error.to_string());
                return Err(error);
            }
            p = self.store.get(id)?;
            if p.txid.is_some() {
                return self.publish(id);
            }
        }
    }

    /// Operator-only recovery of a previously received, durably saved CREATE
    /// response. This never replays create or imports final transaction bytes.
    pub fn reconcile_create(&self, id: &str, response: &std::path::Path) -> Result<Payment> {
        let p = self.store.get(id)?;
        if p.stage != "pending" || p.inflight.as_deref() != Some("create") || p.txid.is_some() {
            return Err(Error::Reconciliation(
                "not a recoverable saved CREATE response".into(),
            ));
        }
        let _owner = p
            .owner
            .as_deref()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| Error::Reconciliation("missing existing owner".into()))?;
        let binding = self.store.create_binding(id)?;
        let source = std::fs::read(response)?;
        if source.len() > 16 * 1024 * 1024 {
            return Err(Error::Invalid("saved response exceeds limit".into()));
        }
        let pczt = self.wallet.recover_created(response, &p.intent, &binding)?;
        if std::fs::read(response)? != source {
            return Err(Error::Integrity);
        }
        self.store
            .reconcile_created(id, &binding, &pczt, &crate::intent::digest(&source))?;
        self.store.get(id)
    }

    pub fn refresh(&self, id: &str, confirmations: u32) -> Result<Payment> {
        if confirmations == 0 || confirmations > 100 {
            return Err(Error::Invalid("confirmation depth must be 1..100".into()));
        }
        let payment = self.store.get(id)?;
        let txid = payment
            .txid
            .as_deref()
            .ok_or_else(|| Error::Reconciliation("no committed transaction to observe".into()))?;
        let mut observation = self.node.observe(txid, confirmations)?;
        let previous_block = payment
            .chain_observation
            .as_ref()
            .and_then(|v| {
                v["blockhash"]
                    .as_str()
                    .or(v["last_known_mined_block"].as_str())
            })
            .map(str::to_owned);
        let current_block = observation["blockhash"].as_str().map(str::to_owned);
        let lost_mining = previous_block.is_some() && current_block != previous_block;
        observation["last_known_mined_block"] = serde_json::json!(current_block.or(previous_block));
        observation["prior_mining_no_longer_observed"] = serde_json::json!(lost_mining);
        observation["mining_reversed"] =
            serde_json::json!(lost_mining && observation["state"] != "unknown");
        self.store.observe(id, "chain_observation", &observation)?;
        self.store.get(id)
    }

    fn publish(&self, id: &str) -> Result<Payment> {
        let p = self.store.get(id)?;
        let candidate = self.store.committed_bytes(id)?;
        let preflight = self.node.preflight(
            &p.intent,
            p.txid
                .as_deref()
                .ok_or_else(|| Error::Reconciliation("missing txid".into()))?,
            &candidate,
        );
        if let Err(error) = preflight {
            self.store.note_error(id, None, &error.to_string())?;
            return Err(error);
        }
        let (attempt, raw, txid) = self.store.start_submission(id)?;
        match self.node.submit(&raw, &txid) {
            Ok(receipt) if receipt == txid => {
                self.store.finish_submission(id, attempt, &receipt)?
            }
            result => {
                let error = match result {
                    Err(e) => e,
                    Ok(_) => Error::Remote("node returned a different transaction ID".into()),
                };
                self.store
                    .note_error(id, Some(attempt), &error.to_string())?;
                return Err(error);
            }
        }
        self.store.get(id)
    }
}

/// Default registration-only mode. There is no force or lease override.
pub struct UnqualifiedWallet;
impl UnqualifiedWallet {
    fn blocked<T>() -> Result<T> {
        Err(Error::Unqualified("real PCZT extraction/restart recovery and payment-effect validation remain unqualified. See docs/qualification-wallet.md".into()))
    }
}
impl Wallet for UnqualifiedWallet {
    fn qualified(&self) -> Result<()> {
        Self::blocked()
    }
    fn create(&self, _: &Intent, _: &OperationBinding) -> Result<Vec<u8>> {
        Self::blocked()
    }
    fn prove(&self, _: &[u8]) -> Result<Vec<u8>> {
        Self::blocked()
    }
    fn sign(&self, _: &[u8], _: &Intent) -> Result<Vec<u8>> {
        Self::blocked()
    }
    fn inspect(&self, _: &[u8], _: &Intent) -> Result<()> {
        Self::blocked()
    }
    fn extract_verified(&self, _: &[u8], _: &Intent) -> Result<(Vec<u8>, String)> {
        Self::blocked()
    }
}
