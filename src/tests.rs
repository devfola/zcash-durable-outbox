//! Local durability tests with explicit fixture adapters. These are NOT Zcash integration proof.
use super::*;
use engine::{Engine, Node, PublicationDisabled, UnqualifiedWallet, Wallet};
use intent::{ExpiryPolicy, FeePolicy, Intent, Recipient};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc, Mutex,
};
use store::{OperationBinding, Store};

fn intent() -> Intent {
    Intent {
        network: "regtest".into(),
        wallet: "sender".into(),
        account: "00000000-0000-0000-0000-000000000001".into(),
        recipient: Recipient {
            address: "uregtest1fixture".into(),
            amount_zat: 100_000,
            memo_hex: "cafe".into(),
        },
        fee: FeePolicy {
            rule: "zip317".into(),
            max_fee_zat: 20_000,
        },
        expiry: ExpiryPolicy {
            min_height: 200,
            max_height: 240,
        },
        privacy: "FullPrivacy".into(),
    }
}
struct FixtureWallet {
    calls: Mutex<Vec<String>>,
    lose: Option<&'static str>,
}
impl FixtureWallet {
    fn new(lose: Option<&'static str>) -> Self {
        Self {
            calls: Mutex::new(vec![]),
            lose,
        }
    }
    fn call(&self, stage: &str) -> Result<Vec<u8>> {
        self.calls.lock().unwrap().push(stage.into());
        if self.lose == Some(stage) {
            return Err(Error::Remote(format!(
                "fixture {stage} acted, response lost"
            )));
        }
        Ok(format!("NOT-A-PCZT:{stage}").into_bytes())
    }
    fn count(&self) -> usize {
        self.calls.lock().unwrap().len()
    }
}
impl Wallet for FixtureWallet {
    fn qualified(&self) -> Result<()> {
        Ok(())
    }
    fn create(&self, _: &Intent, binding: &OperationBinding) -> Result<Vec<u8>> {
        let mut pczt = self.call("create")?;
        pczt.extend_from_slice(binding.operation_id.as_bytes());
        Ok(pczt)
    }
    fn prove(&self, pczt: &[u8]) -> Result<Vec<u8>> {
        let mut output = self.call("prove")?;
        output.extend_from_slice(pczt);
        Ok(output)
    }
    fn sign(&self, pczt: &[u8], _: &Intent) -> Result<Vec<u8>> {
        let mut output = self.call("sign")?;
        output.extend_from_slice(pczt);
        Ok(output)
    }
    fn inspect(&self, pczt: &[u8], _: &Intent) -> Result<()> {
        if !pczt.starts_with(b"NOT-A-PCZT:") {
            return Err(Error::Integrity);
        }
        Ok(())
    }
    fn extract_verified(&self, pczt: &[u8], _: &Intent) -> Result<(Vec<u8>, String)> {
        let mut raw = self.call("extract")?;
        raw.extend_from_slice(pczt);
        let txid = intent::digest(&raw); // Fixture only; never a Zcash txid implementation.
        Ok((raw, txid))
    }
}
struct FixtureNode {
    sent: Mutex<Vec<Vec<u8>>>,
    lose_next: AtomicBool,
    expired: AtomicBool,
    preflights: AtomicUsize,
}
impl FixtureNode {
    fn new(lose: bool) -> Self {
        Self {
            sent: Mutex::new(vec![]),
            lose_next: AtomicBool::new(lose),
            expired: AtomicBool::new(false),
            preflights: AtomicUsize::new(0),
        }
    }
    fn count(&self) -> usize {
        self.sent.lock().unwrap().len()
    }
}
impl Node for FixtureNode {
    fn preflight(&self, _: &Intent, _: &str, _: &[u8]) -> Result<()> {
        self.preflights.fetch_add(1, Ordering::SeqCst);
        if self.expired.load(Ordering::SeqCst) {
            Err(Error::Reconciliation("fixture expiry/reorg".into()))
        } else {
            Ok(())
        }
    }
    fn submit(&self, raw: &[u8], expected: &str) -> Result<String> {
        self.sent.lock().unwrap().push(raw.to_vec());
        if self.lose_next.swap(false, Ordering::SeqCst) {
            Err(Error::Remote("fixture accepted, response lost".into()))
        } else {
            Ok(expected.into())
        }
    }
}
fn setup(
    lose_wallet: Option<&'static str>,
    lose_node: bool,
) -> (
    tempfile::TempDir,
    Store,
    Arc<FixtureWallet>,
    Arc<FixtureNode>,
    Engine,
) {
    let dir = tempfile::tempdir().unwrap();
    let s = Store::open(dir.path().join("outbox.db"), "sender", &intent().account).unwrap();
    let w = Arc::new(FixtureWallet::new(lose_wallet));
    let n = Arc::new(FixtureNode::new(lose_node));
    let e = Engine::new(s.clone(), w.clone(), n.clone());
    (dir, s, w, n, e)
}

#[test]
fn immutable_intent_canonical_binding_and_integer_validation() {
    let (_d, s, _w, _n, _e) = setup(None, false);
    let original = intent();
    s.submit("bounty:42", original.clone()).unwrap();
    let mut upper = original.clone();
    upper.recipient.memo_hex = "CAFE".into();
    s.submit("bounty:42", upper).unwrap();
    let changes: [fn(&mut Intent); 5] = [
        |p| p.recipient.address = "uregtest1other".into(),
        |p| p.recipient.amount_zat += 1,
        |p| p.recipient.memo_hex = "ffff".into(),
        |p| p.fee.max_fee_zat += 1,
        |p| p.expiry.max_height += 1,
    ];
    for change in changes {
        let mut p = original.clone();
        change(&mut p);
        assert!(matches!(s.submit("bounty:42", p), Err(Error::Conflict)));
    }
    let mut p = original.clone();
    p.privacy = "AllowRevealedAmounts".into();
    assert!(s.submit("bounty:42", p).is_err());
    for amount in [
        serde_json::json!(true),
        serde_json::json!(-1),
        serde_json::json!(1.5),
    ] {
        let mut json = serde_json::to_value(&original).unwrap();
        json["recipient"]["amount_zat"] = amount;
        assert!(serde_json::from_value::<Intent>(json).is_err());
    }
    let mut unknown = serde_json::to_value(original).unwrap();
    unknown["replacement"] = true.into();
    assert!(serde_json::from_value::<Intent>(unknown).is_err());
}

#[test]
fn completed_request_returns_same_record_without_more_construction_or_submission() {
    let (_d, s, w, n, e) = setup(None, false);
    e.submit_or_return("p1", intent()).unwrap();
    assert_eq!(e.retry("p1").unwrap().stage, "accepted");
    e.submit_or_return("p1", intent()).unwrap();
    e.retry("p1").unwrap();
    assert_eq!(w.count(), 4);
    assert_eq!(n.count(), 1);
    let c = s.connect().unwrap();
    assert_eq!(
        c.query_row("SELECT count(*) FROM artifacts", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        4
    );
}

#[test]
fn accepted_lost_response_restart_resubmits_identical_durable_bytes_no_wallet_calls() {
    let (dir, s, w, n, e) = setup(None, true);
    s.submit("p1", intent()).unwrap();
    assert!(e.retry("p1").is_err());
    assert_eq!(s.get("p1").unwrap().stage, "committed");
    drop(e);
    let reopened = Store::open(dir.path().join("outbox.db"), "sender", &intent().account).unwrap();
    let restarted = Engine::new(reopened, Arc::new(UnqualifiedWallet), n.clone());
    assert_eq!(restarted.retry("p1").unwrap().stage, "accepted");
    assert_eq!(w.count(), 4);
    let sent = n.sent.lock().unwrap();
    assert_eq!(sent.len(), 2);
    assert_eq!(sent[0], sent[1]);
    assert_eq!(s.get("p1").unwrap().submission_attempts, 2);
}

#[test]
fn concurrent_same_id_claims_and_postcommit_retries_never_rebuild() {
    let (_d, s, w, n, e) = setup(None, true);
    s.submit("p1", intent()).unwrap();
    let e = Arc::new(e);
    let barrier = Arc::new(std::sync::Barrier::new(12));
    std::thread::scope(|scope| {
        for _ in 0..12 {
            let e = e.clone();
            let barrier = barrier.clone();
            scope.spawn(move || {
                barrier.wait();
                let _ = e.retry("p1");
            });
        }
    });
    e.retry("p1").unwrap();
    assert_eq!(w.count(), 4);
    let sent = n.sent.lock().unwrap();
    assert!(sent.len() >= 2);
    assert!(sent.iter().all(|b| b == &sent[0]));
}

#[test]
fn distinct_ids_cannot_overlap_wallet_construction() {
    let (_d, s, w, n, e) = setup(None, false);
    s.submit("p1", intent()).unwrap();
    s.submit("p2", intent()).unwrap();
    s.begin_stage("p1", "pending", "create", "remote-still-running")
        .unwrap();
    assert!(matches!(e.retry("p2"), Err(Error::WalletBusy(_))));
    assert!(matches!(e.retry("p1"), Err(Error::Reconciliation(_))));
    assert_eq!(w.count(), 0);
    assert_eq!(n.count(), 0);
}

#[test]
fn separate_payments_release_wallet_only_with_final_commit() {
    let (_d, s, w, n, e) = setup(None, false);
    s.submit("p1", intent()).unwrap();
    s.submit("p2", intent()).unwrap();
    e.retry("p1").unwrap();
    e.retry("p2").unwrap();
    assert_eq!(w.count(), 8);
    assert_eq!(n.count(), 2);
    assert!(s
        .connect()
        .unwrap()
        .query_row("SELECT payment FROM wallet", [], |r| r
            .get::<_, Option<String>>(0))
        .unwrap()
        .is_none());
}

#[test]
fn actual_sqlite_final_commit_rejection_has_zero_submission_and_retains_wallet_fence() {
    let (_d, s, w, n, e) = setup(None, false);
    s.submit("p1", intent()).unwrap();
    s.reject_final_commit.store(true, Ordering::SeqCst);
    assert!(matches!(e.retry("p1"), Err(Error::Sqlite(_))));
    let p = s.get("p1").unwrap();
    assert!(p.txid.is_none());
    assert_eq!(p.inflight.as_deref(), Some("extract"));
    assert_eq!(w.count(), 4);
    assert_eq!(n.count(), 0);
    assert!(matches!(e.retry("p1"), Err(Error::Reconciliation(_))));
    s.submit("p2", intent()).unwrap();
    assert!(matches!(e.retry("p2"), Err(Error::WalletBusy(_))));
    assert_eq!(w.count(), 4);
    assert_eq!(n.count(), 0);
}

#[test]
fn every_lost_precommit_response_blocks_restart_and_retains_completed_artifacts() {
    for (idx, stage) in ["create", "prove", "sign", "extract"].iter().enumerate() {
        let (dir, s, w, n, e) = setup(Some(stage), false);
        s.submit("p1", intent()).unwrap();
        assert!(e.retry("p1").is_err());
        let p = s.get("p1").unwrap();
        assert_eq!(p.inflight.as_deref(), Some(*stage));
        assert_eq!(
            s.connect()
                .unwrap()
                .query_row("SELECT count(*) FROM artifacts", [], |r| r
                    .get::<_, usize>(0))
                .unwrap(),
            idx
        );
        let before = w.count();
        drop(e);
        let restarted = Engine::new(
            Store::open(dir.path().join("outbox.db"), "sender", &intent().account).unwrap(),
            w.clone(),
            n.clone(),
        );
        assert!(matches!(
            restarted.retry("p1"),
            Err(Error::Reconciliation(_))
        ));
        assert_eq!(w.count(), before);
        assert_eq!(n.count(), 0);
    }
}

#[test]
fn completed_artifact_resumes_under_same_owner_without_repeating_create() {
    let (_d, s, w, n, e) = setup(None, false);
    s.submit("p1", intent()).unwrap();
    s.begin_stage("p1", "pending", "create", "dead-worker")
        .unwrap();
    s.finish_stage(
        "p1",
        "create",
        "created",
        "dead-worker",
        b"NOT-A-PCZT:create",
    )
    .unwrap();
    e.retry("p1").unwrap();
    assert_eq!(s.get("p1").unwrap().owner.as_deref(), Some("dead-worker"));
    assert_eq!(w.count(), 3);
    assert_eq!(n.count(), 1);
}

#[test]
fn saved_create_reconciliation_requires_original_owner_stage_and_wallet_slot() {
    let (_d, s, w, n, e) = setup(None, false);
    s.submit("p1", intent()).unwrap();
    s.begin_stage("p1", "pending", "create", "original-owner")
        .unwrap();
    let binding = s.create_binding("p1").unwrap();
    let mut other_owner = binding.clone();
    other_owner.owner = "other-owner".into();
    assert!(s
        .reconcile_created("p1", &other_owner, b"NOT-A-PCZT:create", "source")
        .is_err());
    let c = s.connect().unwrap();
    c.execute("UPDATE wallet SET payment='different-id' WHERE id=1", [])
        .unwrap();
    assert!(s
        .reconcile_created("p1", &binding, b"NOT-A-PCZT:create", "source")
        .is_err());
    assert_eq!(s.get("p1").unwrap().inflight.as_deref(), Some("create"));
    assert_eq!(
        c.query_row("SELECT count(*) FROM artifacts", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    c.execute("UPDATE wallet SET payment='p1' WHERE id=1", [])
        .unwrap();
    s.reconcile_created("p1", &binding, b"NOT-A-PCZT:create", "source")
        .unwrap();
    assert_eq!(
        s.get("p1").unwrap().owner.as_deref(),
        Some("original-owner")
    );
    assert!(s
        .reconcile_created("p1", &binding, b"NOT-A-PCZT:create", "source")
        .is_err());
    e.retry("p1").unwrap();
    assert_eq!(w.count(), 3);
    assert_eq!(n.count(), 1);
    assert!(s
        .reconcile_created("p1", &binding, b"NOT-A-PCZT:create", "source")
        .is_err());
}

/// Local adapter fixture: no Zcash keys, PCZTs, transaction proofs or chain claims.
/// Exercises the durable Engine/Store binding and recovery across distinct IDs.
#[test]
fn saved_create_cannot_cross_identical_payment_ids_and_original_recovers_without_create() {
    struct SavedCreateWallet {
        store: Store,
        evidence: std::path::PathBuf,
        inner: FixtureWallet,
        creates: AtomicUsize,
    }
    impl Wallet for SavedCreateWallet {
        fn qualified(&self) -> Result<()> {
            Ok(())
        }
        fn create(&self, intent: &Intent, binding: &OperationBinding) -> Result<Vec<u8>> {
            // Observe through a fresh connection: binding and fence must already be durable.
            assert_eq!(self.store.create_binding(&binding.payment_id)?, *binding);
            let payment = self.store.get(&binding.payment_id)?;
            assert_eq!(payment.inflight.as_deref(), Some("create"));
            assert_eq!(payment.owner.as_deref(), Some(binding.owner.as_str()));
            binding.validate(intent)?;
            self.creates.fetch_add(1, Ordering::SeqCst);
            let pczt = self.inner.create(intent, binding)?;
            std::fs::write(
                self.evidence.join(&binding.payment_id),
                serde_json::to_vec(&serde_json::json!({"binding":binding,"pczt":pczt}))?,
            )?;
            if binding.payment_id == "B" {
                Err(Error::Remote(
                    "fixture CREATE response saved; result not committed".into(),
                ))
            } else {
                Ok(pczt)
            }
        }
        fn recover_created(
            &self,
            response: &std::path::Path,
            intent: &Intent,
            expected: &OperationBinding,
        ) -> Result<Vec<u8>> {
            let value: serde_json::Value = serde_json::from_slice(&std::fs::read(response)?)?;
            let binding: OperationBinding = serde_json::from_value(value["binding"].clone())?;
            binding.validate(intent)?;
            if &binding != expected {
                return Err(Error::Integrity);
            }
            Ok(serde_json::from_value(value["pczt"].clone())?)
        }
        fn prove(&self, pczt: &[u8]) -> Result<Vec<u8>> {
            self.inner.prove(pczt)
        }
        fn sign(&self, pczt: &[u8], intent: &Intent) -> Result<Vec<u8>> {
            self.inner.sign(pczt, intent)
        }
        fn inspect(&self, pczt: &[u8], intent: &Intent) -> Result<()> {
            self.inner.inspect(pczt, intent)
        }
        fn extract_verified(&self, pczt: &[u8], intent: &Intent) -> Result<(Vec<u8>, String)> {
            self.inner.extract_verified(pczt, intent)
        }
    }
    let (dir, store, _, node, _) = setup(None, false);
    let wallet = Arc::new(SavedCreateWallet {
        store: store.clone(),
        evidence: dir.path().into(),
        inner: FixtureWallet::new(None),
        creates: AtomicUsize::new(0),
    });
    let engine = Engine::new(store.clone(), wallet.clone(), node.clone());
    store.submit("A", intent()).unwrap();
    store.submit("B", intent()).unwrap();
    engine.retry("A").unwrap(); // A releases its wallet slot only with final COMMIT.
    let original_a = store.get("A").unwrap();
    let raw_a = store.committed_bytes("A").unwrap();
    assert!(engine.retry("B").is_err());
    let a = store.create_binding("A").unwrap();
    let b = store.create_binding("B").unwrap();
    assert_eq!(a.intent_digest, b.intent_digest);
    assert_ne!(a.operation_id, b.operation_id);
    assert_ne!(a.payment_id, b.payment_id);
    let before = store.get("B").unwrap();
    assert!(engine.reconcile_create("B", &dir.path().join("A")).is_err());
    assert_eq!(node.count(), 1);
    assert_eq!(wallet.creates.load(Ordering::SeqCst), 2);
    assert_eq!(store.get("B").unwrap().stage, "pending");
    assert_eq!(store.get("B").unwrap().inflight.as_deref(), Some("create"));
    assert_eq!(store.get("B").unwrap().owner, before.owner);
    assert!(store.artifact("B", "created").is_err());
    assert_eq!(
        store
            .connect()
            .unwrap()
            .query_row("SELECT payment FROM wallet WHERE id=1", [], |r| r
                .get::<_, String>(0))
            .unwrap(),
        "B"
    );
    assert_eq!(store.get("A").unwrap().txid, original_a.txid);
    assert_eq!(store.get("A").unwrap().byte_digest, original_a.byte_digest);
    assert_eq!(store.committed_bytes("A").unwrap(), raw_a);
    // Even a caller that bypassed adapter verification cannot replace the durable operation.
    assert!(store
        .reconcile_created("B", &a, b"NOT-A-PCZT:create", "wrong-response")
        .is_err());
    let recovered = engine.reconcile_create("B", &dir.path().join("B")).unwrap();
    assert_eq!(recovered.stage, "created");
    assert_eq!(recovered.owner, before.owner);
    let saved_b: serde_json::Value =
        serde_json::from_slice(&std::fs::read(dir.path().join("B")).unwrap()).unwrap();
    assert_eq!(
        store.artifact("B", "created").unwrap(),
        serde_json::from_value::<Vec<u8>>(saved_b["pczt"].clone()).unwrap()
    );
    assert_eq!(wallet.creates.load(Ordering::SeqCst), 2);
    // A restart resumes from the recovered artifact, with no third CREATE.
    let restarted = Engine::new(
        Store::open(dir.path().join("outbox.db"), "sender", &intent().account).unwrap(),
        wallet.clone(),
        node.clone(),
    );
    restarted.retry("B").unwrap();
    assert_eq!(wallet.creates.load(Ordering::SeqCst), 2);
    assert_eq!(node.count(), 2);
    assert_ne!(store.get("A").unwrap().txid, store.get("B").unwrap().txid);
    let sent = node.sent.lock().unwrap();
    assert_eq!(sent[0], raw_a);
    assert_eq!(sent[1], store.committed_bytes("B").unwrap());
    let c = store.connect().unwrap();
    assert!(c
        .execute(
            "UPDATE create_operations SET payment_id='B' WHERE payment_id='A'",
            []
        )
        .is_err());
    assert!(c
        .execute("DELETE FROM create_operations WHERE payment_id='A'", [])
        .is_err());
}

#[test]
fn sqlite_enforces_final_immutability_and_corruption_is_detected_even_after_acceptance() {
    for lost in [false, true] {
        let (_d, s, w, n, e) = setup(None, lost);
        s.submit("p1", intent()).unwrap();
        let _ = e.retry("p1");
        let before = n.count();
        let c = s.connect().unwrap();
        assert!(c
            .execute("UPDATE payments SET raw=X'00' WHERE id='p1'", [])
            .is_err());
        c.execute_batch(
            "DROP TRIGGER final_immutable; UPDATE payments SET raw=X'00' WHERE id='p1';",
        )
        .unwrap();
        assert!(matches!(s.get("p1"), Err(Error::Integrity)));
        assert!(matches!(e.retry("p1"), Err(Error::Integrity)));
        assert_eq!(w.count(), 4);
        assert_eq!(n.count(), before);
    }
}

#[test]
fn expiry_or_reorg_observation_never_triggers_replacement() {
    let (_d, s, w, n, e) = setup(None, true);
    s.submit("p1", intent()).unwrap();
    let _ = e.retry("p1");
    n.expired.store(true, Ordering::SeqCst);
    assert!(matches!(e.retry("p1"), Err(Error::Reconciliation(_))));
    assert_eq!(w.count(), 4);
    assert_eq!(n.count(), 1);
    assert!(s.get("p1").unwrap().txid.is_some());
}

#[test]
fn bundled_adapter_is_fail_closed_without_claim_or_network_effects() {
    let (_d, s, _w, n, _e) = setup(None, false);
    s.submit("p1", intent()).unwrap();
    let e = Engine::new(s.clone(), Arc::new(UnqualifiedWallet), n.clone());
    assert!(matches!(e.retry("p1"), Err(Error::Unqualified(_))));
    let p = s.get("p1").unwrap();
    assert!(p.owner.is_none());
    assert!(p.inflight.is_none());
    assert_eq!(n.count(), 0);
}

#[test]
fn executable_publication_gate_rejects_preexisting_committed_bytes_before_attempt() {
    let (_d, s, w, n, e) = setup(None, true);
    s.submit("p1", intent()).unwrap();
    assert!(e.retry("p1").is_err());
    assert_eq!(s.get("p1").unwrap().stage, "committed");
    let before = s.get("p1").unwrap().submission_attempts;
    let guarded = Engine::new(
        s.clone(),
        Arc::new(UnqualifiedWallet),
        Arc::new(PublicationDisabled),
    );
    assert!(matches!(guarded.retry("p1"), Err(Error::Unqualified(_))));
    assert_eq!(s.get("p1").unwrap().submission_attempts, before);
    assert_eq!(w.count(), 4);
    assert_eq!(n.count(), 1);
}

#[test]
fn endpoint_policy_rejects_remote_targets_and_redirects() {
    assert!(rpc::Rpc::new("http://8.8.8.8:18232", None).is_err());
    assert!(rpc::Rpc::new("http://example.org:18232", None).is_err());
    assert!(rpc::Rpc::new("https://127.0.0.1:18232", None).is_err());
    assert!(rpc::loopback_bind("0.0.0.0:17840").is_err());
    assert!(rpc::Rpc::new("http://127.0.0.1:18232", None).is_ok());
}

#[test]
fn persisted_intent_digest_detects_corruption_and_recovery_view_omits_artifacts() {
    let (_d, s, _w, _n, e) = setup(None, false);
    let original = s.submit("p1", intent()).unwrap();
    assert_eq!(
        original.intent_digest,
        intent::digest(original.intent.canonical_json().unwrap().as_bytes())
    );
    e.retry("p1").unwrap();
    let evidence = s.evidence("p1").unwrap();
    assert_eq!(evidence["artifacts"].as_array().unwrap().len(), 4);
    assert!(evidence["artifacts"].as_array().unwrap().iter().all(|v| v
        .get("bytes")
        .unwrap()
        .is_number()
        && v.get("sha256").unwrap().is_string()));
    let c = s.connect().unwrap();
    assert!(c
        .execute("UPDATE payments SET intent_digest='bad' WHERE id='p1'", [])
        .is_err());
    c.execute_batch("DROP TRIGGER intent_digest_immutable; UPDATE payments SET intent_digest='bad' WHERE id='p1';").unwrap();
    assert!(matches!(s.get("p1"), Err(Error::Integrity)));
}

#[test]
fn schema_one_migration_preserves_payment_and_backfills_digest() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("old.db");
    let c = rusqlite::Connection::open(&path).unwrap();
    c.execute_batch(include_str!("schema.sql")).unwrap();
    c.execute(
        "INSERT INTO wallet(id,name,account,network) VALUES(1,?,?, 'regtest')",
        rusqlite::params![intent().wallet, intent().account],
    )
    .unwrap();
    c.execute(
        "INSERT INTO payments(id,intent) VALUES('old',?)",
        [intent().canonical_json().unwrap()],
    )
    .unwrap();
    c.pragma_update(None, "user_version", 1).unwrap();
    drop(c);
    let s = Store::open(&path, "sender", &intent().account).unwrap();
    let p = s.get("old").unwrap();
    assert_eq!(
        p.intent_digest,
        intent::digest(p.intent.canonical_json().unwrap().as_bytes())
    );
    assert!(p.owner.is_none());
    assert_eq!(
        s.connect()
            .unwrap()
            .query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        3
    );
}

#[test]
fn actual_v6_header_uses_chosen_expiry_within_approved_range() {
    let mut approved = intent();
    approved.expiry.min_height = 108;
    approved.expiry.max_height = 207;
    let header = hex::decode("0600008098b684d85b16a5370000000094000000").unwrap();
    assert_eq!(rpc::verified_v6_expiry(&header, &approved).unwrap(), 148);
    assert!(rpc::verified_v6_expiry(&header[..19], &approved).is_err());
    approved.expiry.max_height = 147;
    assert!(rpc::verified_v6_expiry(&header, &approved).is_err());
}

#[test]
fn preflight_failure_persists_diagnostics_without_submission_or_replacement() {
    let (dir, s, w, n, e) = setup(None, true);
    e.submit_or_return("p1", intent()).unwrap();
    assert!(e.retry("p1").is_err());
    let original = s.get("p1").unwrap();
    let committed = s.committed_bytes("p1").unwrap();
    n.expired.store(true, Ordering::SeqCst);
    assert!(matches!(e.retry("p1"), Err(Error::Reconciliation(_))));
    drop(e);

    let reopened = Store::open(dir.path().join("outbox.db"), "sender", &intent().account).unwrap();
    let failed = reopened.get("p1").unwrap();
    assert_eq!(failed.stage, "committed");
    assert_eq!(failed.txid, original.txid);
    assert_eq!(failed.submission_attempts, original.submission_attempts);
    assert!(failed.last_error.unwrap().contains("fixture expiry/reorg"));
    assert_eq!(reopened.committed_bytes("p1").unwrap(), committed);
    let evidence = reopened.evidence("p1").unwrap();
    assert_eq!(evidence["attempts"].as_array().unwrap().len(), 1);
    assert_eq!(evidence["attempts"][0]["outcome"], "unknown");
    let last = evidence["events"].as_array().unwrap().last().unwrap();
    assert_eq!(last["kind"], "operation_uncertain");
    assert!(last["detail"]
        .as_str()
        .unwrap()
        .contains("fixture expiry/reorg"));
    assert_eq!(w.count(), 4);
    assert_eq!(n.count(), 1);
}

#[test]
fn unknown_observation_keeps_mined_history_for_later_reorg_evidence() {
    struct Observations(Mutex<std::collections::VecDeque<serde_json::Value>>, String);
    impl Node for Observations {
        fn preflight(&self, _: &Intent, _: &str, _: &[u8]) -> Result<()> {
            panic!("refresh must never preflight a submission")
        }
        fn submit(&self, _: &[u8], _: &str) -> Result<String> {
            panic!("refresh must never publish")
        }
        fn observe(&self, txid: &str, confirmations: u32) -> Result<serde_json::Value> {
            assert_eq!(txid, self.1);
            assert_eq!(confirmations, 3);
            Ok(self.0.lock().unwrap().pop_front().unwrap())
        }
    }

    let (dir, s, w, n, e) = setup(None, false);
    e.submit_or_return("p1", intent()).unwrap();
    e.retry("p1").unwrap();
    let committed = s.committed_bytes("p1").unwrap();
    let block = "cd".repeat(32);
    let observer = Arc::new(Observations(
        Mutex::new(
            [
                serde_json::json!({"state":"confirmed","blockhash":block,"confirmations":3}),
                serde_json::json!({"state":"unknown","blockhash":null,"confirmations":0}),
                serde_json::json!({"state":"node_observed","blockhash":null,"confirmations":0}),
            ]
            .into(),
        ),
        s.get("p1").unwrap().txid.unwrap(),
    ));
    let refreshed = Engine::new(s.clone(), Arc::new(UnqualifiedWallet), observer);
    let mined = refreshed.refresh("p1", 3).unwrap();
    assert_eq!(
        mined.chain_observation.unwrap()["last_known_mined_block"],
        block
    );
    let unknown = refreshed
        .refresh("p1", 3)
        .unwrap()
        .chain_observation
        .unwrap();
    assert_eq!(unknown["last_known_mined_block"], block);
    assert_eq!(unknown["prior_mining_no_longer_observed"], true);
    assert_eq!(unknown["mining_reversed"], false);
    refreshed.refresh("p1", 3).unwrap();
    drop(refreshed);

    let reopened = Store::open(dir.path().join("outbox.db"), "sender", &intent().account).unwrap();
    let payment = reopened.get("p1").unwrap();
    assert_eq!(payment.stage, "accepted");
    assert_eq!(payment.submission_attempts, 1);
    let observed = payment.chain_observation.unwrap();
    assert_eq!(observed["last_known_mined_block"], block);
    assert_eq!(observed["mining_reversed"], true);
    let history = reopened.evidence("p1").unwrap();
    assert_eq!(
        history["events"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|event| event["kind"] == "chain_observation")
            .count(),
        3
    );
    assert_eq!(reopened.committed_bytes("p1").unwrap(), committed);
    assert_eq!(w.count(), 4);
    assert_eq!(n.count(), 1);
}
