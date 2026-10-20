use crate::{
    intent::{digest, validate_id, Intent},
    Error, Result,
};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

#[derive(Clone)]
pub struct Store {
    path: PathBuf,
    #[cfg(test)]
    pub reject_final_commit: std::sync::Arc<std::sync::atomic::AtomicBool>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Payment {
    pub id: String,
    pub intent: Intent,
    pub intent_digest: String,
    pub stage: String,
    pub inflight: Option<String>,
    pub owner: Option<String>,
    pub txid: Option<String>,
    pub byte_digest: Option<String>,
    pub last_error: Option<String>,
    pub submission_attempts: i64,
    pub chain_observation: Option<serde_json::Value>,
}

/// Persisted before CREATE and copied verbatim into that operation's evidence.
/// Intent equality alone cannot identify the business payment that invoked CREATE.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OperationBinding {
    pub operation_id: String,
    pub payment_id: String,
    pub owner: String,
    pub intent_digest: String,
}

impl OperationBinding {
    pub fn validate(&self, intent: &Intent) -> Result<()> {
        validate_id(&self.payment_id)?;
        validate_id(&self.owner)?;
        let operation_id =
            uuid::Uuid::parse_str(&self.operation_id).map_err(|_| Error::Integrity)?;
        if operation_id.to_string() != self.operation_id
            || self.intent_digest != digest(intent.canonical_json()?.as_bytes())
        {
            return Err(Error::Integrity);
        }
        Ok(())
    }
}

impl Store {
    pub fn open(path: impl AsRef<Path>, wallet: &str, account: &str) -> Result<Self> {
        validate_id(wallet)?;
        let account = uuid::Uuid::parse_str(account)
            .map_err(|_| Error::Invalid("account UUID required".into()))?
            .to_string();
        let path = path.as_ref();
        if path == Path::new(":memory:") {
            return Err(Error::Invalid("a durable database file is required".into()));
        }
        let s = Self {
            path: path.into(),
            #[cfg(test)]
            reject_final_commit: Default::default(),
        };
        let mut c = s.connect()?;
        c.pragma_update(None, "journal_mode", "WAL")?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        // Read under the migration lock so concurrent openers cannot both migrate.
        let version: i64 = tx.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        if version > 3 {
            return Err(Error::Invalid("unsupported database schema".into()));
        }
        tx.execute_batch(include_str!("schema.sql"))?;
        if version < 2 {
            tx.execute_batch("ALTER TABLE payments ADD COLUMN intent_digest TEXT;")?;
            let records = {
                let mut query = tx.prepare("SELECT id,intent FROM payments")?;
                let rows = query
                    .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
                rows.collect::<std::result::Result<Vec<_>, _>>()?
            };
            for (id, canonical) in records {
                tx.execute(
                    "UPDATE payments SET intent_digest=? WHERE id=?",
                    params![digest(canonical.as_bytes()), id],
                )?;
            }
        }
        tx.execute_batch("CREATE TRIGGER IF NOT EXISTS intent_digest_immutable BEFORE UPDATE OF intent_digest ON payments BEGIN SELECT RAISE(ABORT,'immutable intent digest'); END;")?;
        crate::store_migration::ensure_txid_uniqueness(&tx)?;
        tx.execute(
            "INSERT OR IGNORE INTO wallet(id,name,account,network) VALUES(1,?,?,'regtest')",
            params![wallet, account],
        )?;
        let identity: (String, String) =
            tx.query_row("SELECT name,account FROM wallet WHERE id=1", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })?;
        if identity != (wallet.into(), account) {
            return Err(Error::Invalid(
                "database belongs to a different wallet/account".into(),
            ));
        }
        tx.pragma_update(None, "user_version", 3)?;
        tx.commit()?;
        Ok(s)
    }

    pub(crate) fn connect(&self) -> Result<Connection> {
        let c = Connection::open(&self.path)?;
        c.busy_timeout(Duration::from_secs(10))?;
        c.pragma_update(None, "foreign_keys", true)?;
        c.pragma_update(None, "synchronous", "FULL")?;
        Ok(c)
    }

    pub fn submit(&self, id: &str, intent: Intent) -> Result<Payment> {
        validate_id(id)?;
        let intent = intent.canonicalize()?;
        let canonical = intent.canonical_json()?;
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let identity: (String, String) =
            tx.query_row("SELECT name,account FROM wallet WHERE id=1", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })?;
        if identity != (intent.wallet.clone(), intent.account.clone()) {
            return Err(Error::Invalid(
                "intent wallet/account differs from the dedicated wallet".into(),
            ));
        }
        tx.execute(
            "INSERT OR IGNORE INTO payments(id,intent,intent_digest) VALUES(?,?,?)",
            params![id, canonical, digest(canonical.as_bytes())],
        )?;
        let old: String =
            tx.query_row("SELECT intent FROM payments WHERE id=?", [id], |r| r.get(0))?;
        if old != canonical {
            return Err(Error::Conflict);
        }
        tx.commit()?;
        self.get(id)
    }

    pub fn get(&self, id: &str) -> Result<Payment> {
        let c = self.connect()?;
        let row = c.query_row("SELECT id,intent,stage,inflight,owner,txid,byte_digest,last_error,(SELECT count(*) FROM attempts WHERE payment=p.id) FROM payments p WHERE id=?", [id], |r| {
            Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,Option<String>>(3)?,r.get::<_,Option<String>>(4)?,r.get::<_,Option<String>>(5)?,r.get::<_,Option<String>>(6)?,r.get::<_,Option<String>>(7)?,r.get::<_,i64>(8)?))
        }).optional()?.ok_or(Error::NotFound)?;
        if row.5.is_some() {
            self.verify_committed(id)?;
        }
        let intent_digest: Option<String> =
            c.query_row("SELECT intent_digest FROM payments WHERE id=?", [id], |r| {
                r.get(0)
            })?;
        let intent_digest = intent_digest.ok_or(Error::Integrity)?;
        if intent_digest != digest(row.1.as_bytes()) {
            return Err(Error::Integrity);
        }
        let observation: Option<String> = c.query_row("SELECT detail FROM events WHERE payment=? AND kind='chain_observation' ORDER BY id DESC LIMIT 1",[id],|r|r.get(0)).optional()?;
        Ok(Payment {
            id: row.0,
            intent: serde_json::from_str(&row.1)?,
            intent_digest,
            stage: row.2,
            inflight: row.3,
            owner: row.4,
            txid: row.5,
            byte_digest: row.6,
            last_error: row.7,
            submission_attempts: row.8,
            chain_observation: observation.map(|v| serde_json::from_str(&v)).transpose()?,
        })
    }

    pub fn verify_committed(&self, id: &str) -> Result<()> {
        let c = self.connect()?;
        let (raw, hash): (Option<Vec<u8>>, Option<String>) = c
            .query_row(
                "SELECT raw,byte_digest FROM payments WHERE id=?",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?
            .ok_or(Error::NotFound)?;
        match (raw, hash) {
            (Some(raw), Some(hash)) if digest(&raw) == hash => Ok(()),
            _ => Err(Error::Integrity),
        }
    }

    pub fn committed_bytes(&self, id: &str) -> Result<Vec<u8>> {
        let c = self.connect()?;
        let (raw, hash): (Option<Vec<u8>>, Option<String>) = c
            .query_row(
                "SELECT raw,byte_digest FROM payments WHERE id=?",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?
            .ok_or(Error::NotFound)?;
        match (raw, hash) {
            (Some(raw), Some(hash)) if digest(&raw) == hash => Ok(raw),
            _ => Err(Error::Integrity),
        }
    }

    pub fn unresolved(&self) -> Result<Vec<Payment>> {
        let c = self.connect()?;
        // Node acceptance is unresolved settlement; keep accepted payments visible.
        let mut q = c.prepare("SELECT id FROM payments ORDER BY rowid")?;
        let ids = q
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        ids.iter().map(|id| self.get(id)).collect()
    }

    /// Commit a wallet-wide owner and stage marker BEFORE any remote mutation.
    pub(crate) fn begin_stage(
        &self,
        id: &str,
        expected: &str,
        operation: &str,
        owner: &str,
    ) -> Result<()> {
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (stage, inflight, prior_owner, raw): (
            String,
            Option<String>,
            Option<String>,
            Option<Vec<u8>>,
        ) = tx
            .query_row(
                "SELECT stage,inflight,owner,raw FROM payments WHERE id=?",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()?
            .ok_or(Error::NotFound)?;
        if raw.is_some() {
            return Err(Error::Invalid(
                "committed payments cannot run construction".into(),
            ));
        }
        if let Some(op) = inflight {
            return Err(Error::Reconciliation(op));
        }
        if stage != expected {
            return Err(Error::Reconciliation("concurrent stage advancement".into()));
        }
        if prior_owner.as_deref().is_some_and(|p| p != owner) {
            return Err(Error::Reconciliation(
                "construction owner survives process death".into(),
            ));
        }
        let held: Option<String> =
            tx.query_row("SELECT payment FROM wallet WHERE id=1", [], |r| r.get(0))?;
        if let Some(held) = held {
            if held != id {
                return Err(Error::WalletBusy(held));
            }
        }
        tx.execute("UPDATE wallet SET payment=? WHERE id=1", [id])?;
        tx.execute(
            "UPDATE payments SET owner=?,inflight=?,last_error=NULL WHERE id=?",
            params![owner, operation, id],
        )?;
        if operation == "create" {
            if expected != "pending" {
                return Err(Error::Reconciliation(
                    "CREATE requires pending state".into(),
                ));
            }
            let canonical: String =
                tx.query_row("SELECT intent FROM payments WHERE id=?", [id], |r| r.get(0))?;
            let binding = OperationBinding {
                operation_id: uuid::Uuid::new_v4().to_string(),
                payment_id: id.into(),
                owner: owner.into(),
                intent_digest: digest(canonical.as_bytes()),
            };
            binding.validate(&serde_json::from_str(&canonical)?)?;
            // This insertion and the wallet-wide fence COMMIT before CREATE.
            // Existing uncertain operations are never assigned a new binding.
            tx.execute(
                "INSERT INTO create_operations(operation_id,payment_id,owner,intent_digest) VALUES(?,?,?,?)",
                params![binding.operation_id,binding.payment_id,binding.owner,binding.intent_digest],
            )?;
            tx.execute(
                "INSERT INTO events(payment,kind,detail) VALUES(?,'create_operation_bound',?)",
                params![id, serde_json::to_string(&binding)?],
            )?;
        }
        tx.execute(
            "INSERT INTO events(payment,kind,detail) VALUES(?,'stage_started',?)",
            params![id, operation],
        )?;
        tx.commit()?;
        Ok(())
    }

    pub(crate) fn create_binding(&self, id: &str) -> Result<OperationBinding> {
        let payment = self.get(id)?;
        let binding = Self::read_create_binding(&self.connect()?, id)?;
        binding.validate(&payment.intent)?;
        if binding.payment_id != payment.id
            || Some(binding.owner.as_str()) != payment.owner.as_deref()
            || binding.intent_digest != payment.intent_digest
        {
            return Err(Error::Integrity);
        }
        Ok(binding)
    }

    fn read_create_binding(c: &Connection, id: &str) -> Result<OperationBinding> {
        c.query_row(
            "SELECT operation_id,payment_id,owner,intent_digest FROM create_operations WHERE payment_id=?",
            [id],
            |r| Ok(OperationBinding {operation_id:r.get(0)?,payment_id:r.get(1)?,owner:r.get(2)?,intent_digest:r.get(3)?}),
        ).optional()?.ok_or_else(|| Error::Reconciliation(
            "saved CREATE has no verifiable original operation binding; retain quarantine".into(),
        ))
    }

    pub(crate) fn artifact(&self, id: &str, stage: &str) -> Result<Vec<u8>> {
        let c = self.connect()?;
        let (bytes, hash): (Vec<u8>, String) = c.query_row(
            "SELECT bytes,digest FROM artifacts WHERE payment=? AND stage=?",
            params![id, stage],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if digest(&bytes) != hash {
            return Err(Error::Integrity);
        }
        Ok(bytes)
    }

    pub(crate) fn finish_stage(
        &self,
        id: &str,
        operation: &str,
        next: &str,
        owner: &str,
        bytes: &[u8],
    ) -> Result<()> {
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let n = tx.execute("UPDATE payments SET stage=?,inflight=NULL WHERE id=? AND inflight=? AND owner=? AND raw IS NULL", params![next,id,operation,owner])?;
        if n != 1 {
            return Err(Error::Reconciliation("ownership fence lost".into()));
        }
        tx.execute(
            "INSERT INTO artifacts(payment,stage,bytes,digest) VALUES(?,?,?,?)",
            params![id, next, bytes, digest(bytes)],
        )?;
        tx.execute(
            "INSERT INTO events(payment,kind,detail) VALUES(?,'stage_completed',?)",
            params![id, operation],
        )?;
        tx.commit()?;
        Ok(())
    }

    pub(crate) fn commit_final(
        &self,
        id: &str,
        owner: &str,
        bytes: &[u8],
        txid: &str,
    ) -> Result<()> {
        if bytes.is_empty() || txid.len() != 64 || hex::decode(txid).is_err() {
            return Err(Error::Invalid(
                "adapter must return transaction bytes and a real Zcash txid".into(),
            ));
        }
        let txid = txid.to_ascii_lowercase();
        let mut c = self.connect()?;
        #[cfg(test)]
        if self
            .reject_final_commit
            .swap(false, std::sync::atomic::Ordering::SeqCst)
        {
            // SQLite itself rejects the transaction at COMMIT. This is not an early-return mock.
            c.commit_hook(Some(|| true));
        }
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let n = tx.execute("UPDATE payments SET raw=?,byte_digest=?,txid=?,stage='committed',inflight=NULL,last_error=NULL WHERE id=? AND owner=? AND inflight='extract' AND raw IS NULL", params![bytes,digest(bytes),txid,id,owner])?;
        if n != 1 {
            return Err(Error::Reconciliation("final ownership fence lost".into()));
        }
        tx.execute(
            "INSERT INTO artifacts(payment,stage,bytes,digest) VALUES(?,'extracted',?,?)",
            params![id, bytes, digest(bytes)],
        )?;
        if tx.execute(
            "UPDATE wallet SET payment=NULL WHERE id=1 AND payment=?",
            [id],
        )? != 1
        {
            return Err(Error::Reconciliation("wallet ownership fence lost".into()));
        }
        tx.execute(
            "INSERT INTO events(payment,kind,detail) VALUES(?,'final_bytes_committed',?)",
            params![id, txid],
        )?;
        tx.commit()?;
        Ok(())
    }

    pub(crate) fn reconcile_created(
        &self,
        id: &str,
        binding: &OperationBinding,
        bytes: &[u8],
        response_digest: &str,
    ) -> Result<()> {
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let original = Self::read_create_binding(&tx, id)?;
        let (canonical, intent_digest): (String, String) = tx.query_row(
            "SELECT intent,intent_digest FROM payments WHERE id=?",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        binding.validate(&serde_json::from_str(&canonical)?)?;
        if &original != binding
            || binding.payment_id != id
            || binding.intent_digest != intent_digest
        {
            return Err(Error::Reconciliation(
                "saved CREATE operation binding differs".into(),
            ));
        }
        let held: Option<String> =
            tx.query_row("SELECT payment FROM wallet WHERE id=1", [], |r| r.get(0))?;
        if held.as_deref() != Some(id) {
            return Err(Error::Reconciliation(
                "wallet ownership fence differs".into(),
            ));
        }
        let changed=tx.execute("UPDATE payments SET stage='created',inflight=NULL,last_error=NULL WHERE id=? AND stage='pending' AND inflight='create' AND owner=? AND raw IS NULL AND txid IS NULL",params![id,binding.owner])?;
        if binding.owner.is_empty() || changed != 1 {
            return Err(Error::Reconciliation(
                "saved CREATE ownership fence differs".into(),
            ));
        }
        tx.execute(
            "INSERT INTO artifacts(payment,stage,bytes,digest) VALUES(?,'created',?,?)",
            params![id, bytes, digest(bytes)],
        )?;
        tx.execute("INSERT INTO events(payment,kind,detail) VALUES(?,'saved_create_response_reconciled',?)",params![id,serde_json::json!({"binding":binding,"pczt_sha256":digest(bytes),"response_sha256":response_digest,"owner_changed":false,"create_replayed":false}).to_string()])?;
        tx.commit()?;
        Ok(())
    }

    /// Fresh durable read for every submission; no caller-supplied transaction bytes.
    pub(crate) fn start_submission(&self, id: &str) -> Result<(i64, Vec<u8>, String)> {
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (raw, hash, txid): (Option<Vec<u8>>, Option<String>, Option<String>) = tx
            .query_row(
                "SELECT raw,byte_digest,txid FROM payments WHERE id=?",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?
            .ok_or(Error::NotFound)?;
        let (raw, hash, txid) = match (raw, hash, txid) {
            (Some(r), Some(h), Some(t)) => (r, h, t),
            _ => return Err(Error::Reconciliation("no committed transaction".into())),
        };
        if digest(&raw) != hash {
            return Err(Error::Integrity);
        }
        tx.execute(
            "INSERT INTO attempts(payment,byte_digest,outcome) VALUES(?,?,'inflight')",
            params![id, hash],
        )?;
        let attempt = tx.last_insert_rowid();
        tx.commit()?;
        Ok((attempt, raw, txid))
    }

    pub(crate) fn finish_submission(&self, id: &str, attempt: i64, receipt: &str) -> Result<()> {
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        tx.execute(
            "UPDATE attempts SET outcome='accepted',detail=? WHERE id=? AND payment=?",
            params![receipt, attempt, id],
        )?;
        tx.execute(
            "UPDATE payments SET stage='accepted',last_error=NULL WHERE id=? AND raw IS NOT NULL",
            [id],
        )?;
        tx.commit()?;
        Ok(())
    }

    pub(crate) fn note_error(&self, id: &str, attempt: Option<i64>, error: &str) -> Result<()> {
        let mut c = self.connect()?;
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        tx.execute(
            "UPDATE payments SET last_error=? WHERE id=?",
            params![error, id],
        )?;
        if let Some(a) = attempt {
            tx.execute(
                "UPDATE attempts SET outcome='unknown',detail=? WHERE id=? AND payment=?",
                params![error, a, id],
            )?;
        }
        tx.execute(
            "INSERT INTO events(payment,kind,detail) VALUES(?,'operation_uncertain',?)",
            params![id, error],
        )?;
        tx.commit()?;
        Ok(())
    }

    pub fn observe(&self, id: &str, kind: &str, detail: &serde_json::Value) -> Result<()> {
        self.get(id)?;
        self.connect()?.execute(
            "INSERT INTO events(payment,kind,detail) VALUES(?,?,?)",
            params![id, kind, detail.to_string()],
        )?;
        Ok(())
    }

    /// Recovery metadata excludes raw PCZTs, ciphertext bodies and credentials.
    pub fn evidence(&self, id: &str) -> Result<serde_json::Value> {
        let payment = self.get(id)?;
        let c = self.connect()?;
        let binding = Self::read_create_binding(&c, id).ok();
        let mut query = c.prepare(
            "SELECT stage,length(bytes),digest FROM artifacts WHERE payment=? ORDER BY rowid",
        )?;
        let artifacts = query.query_map([id],|r|Ok(serde_json::json!({"stage":r.get::<_,String>(0)?,"bytes":r.get::<_,i64>(1)?,"sha256":r.get::<_,String>(2)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        let mut query = c.prepare("SELECT id,byte_digest,outcome,detail,created_at FROM attempts WHERE payment=? ORDER BY id")?;
        let attempts = query.query_map([id],|r|Ok(serde_json::json!({"id":r.get::<_,i64>(0)?,"byte_digest":r.get::<_,String>(1)?,"outcome":r.get::<_,String>(2)?,"detail":r.get::<_,Option<String>>(3)?,"created_at":r.get::<_,String>(4)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        let mut query =
            c.prepare("SELECT id,kind,detail,created_at FROM events WHERE payment=? ORDER BY id")?;
        let events = query.query_map([id],|r|Ok(serde_json::json!({"id":r.get::<_,i64>(0)?,"kind":r.get::<_,String>(1)?,"detail":r.get::<_,String>(2)?,"created_at":r.get::<_,String>(3)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(
            serde_json::json!({"payment":payment,"create_operation":binding,"artifacts":artifacts,"attempts":attempts,"events":events,"automatic_reconciliation":false}),
        )
    }
}
