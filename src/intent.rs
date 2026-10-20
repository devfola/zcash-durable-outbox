use crate::{Error, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const MAX_MONEY: u64 = 2_100_000_000_000_000;

/// Candidate local-regtest intent schema. The live shielded profile remains unqualified.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Intent {
    pub network: String,
    pub wallet: String,
    pub account: String,
    pub recipient: Recipient,
    pub fee: FeePolicy,
    pub expiry: ExpiryPolicy,
    pub privacy: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Recipient {
    pub address: String,
    pub amount_zat: u64,
    /// Exact memo bytes, hex encoded; empty means no user memo.
    pub memo_hex: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FeePolicy {
    pub rule: String,
    pub max_fee_zat: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExpiryPolicy {
    pub min_height: u32,
    pub max_height: u32,
}

impl Intent {
    pub fn canonicalize(mut self) -> Result<Self> {
        if self.network != "regtest" || self.privacy != "FullPrivacy" || self.fee.rule != "zip317" {
            return Err(Error::Invalid(
                "only regtest / FullPrivacy / zip317 is supported".into(),
            ));
        }
        validate_id(&self.wallet)?;
        self.account = uuid::Uuid::parse_str(&self.account)
            .map_err(|_| Error::Invalid("account must be a UUID".into()))?
            .to_string();
        // Full address/network/receiver validation belongs to the qualified PCZT adapter.
        // No unvalidated intent can reach a live wallet in the bundled binary.
        if !self.recipient.address.starts_with("uregtest1")
            || self.recipient.address.len() > 1024
            || !self
                .recipient
                .address
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        {
            return Err(Error::Invalid(
                "a regtest unified address is required".into(),
            ));
        }
        if self.recipient.amount_zat == 0
            || self.recipient.amount_zat > MAX_MONEY
            || self.fee.max_fee_zat == 0
            || self.fee.max_fee_zat > MAX_MONEY
            || self
                .recipient
                .amount_zat
                .checked_add(self.fee.max_fee_zat)
                .is_none_or(|v| v > MAX_MONEY)
        {
            return Err(Error::Invalid(
                "amount and fee must fit MAX_MONEY in integer zatoshi".into(),
            ));
        }
        let memo = hex::decode(&self.recipient.memo_hex)
            .map_err(|_| Error::Invalid("memo_hex must contain whole hex bytes".into()))?;
        if memo.len() > 512 {
            return Err(Error::Invalid("memo exceeds 512 bytes".into()));
        }
        self.recipient.memo_hex = hex::encode(memo);
        if self.expiry.min_height == 0 || self.expiry.min_height > self.expiry.max_height {
            return Err(Error::Invalid(
                "expiry must be a nonzero inclusive height interval".into(),
            ));
        }
        Ok(self)
    }
    pub fn canonical_json(&self) -> Result<String> {
        Ok(serde_json::to_string(self)?)
    }
}

pub fn validate_id(id: &str) -> Result<()> {
    if id.is_empty()
        || id.len() > 128
        || !id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_:.".contains(&b))
    {
        Err(Error::Invalid(
            "IDs must be 1..128 ASCII letters/digits or - _ : .".into(),
        ))
    } else {
        Ok(())
    }
}
pub fn digest(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
