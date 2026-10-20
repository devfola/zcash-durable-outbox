//! SQLite migration/collision tests. Transaction bytes and IDs here are fixtures.
use crate::{
    engine::{Engine, PublicationDisabled, Wallet},
    intent::{digest, ExpiryPolicy, FeePolicy, Intent, Recipient},
    store::{OperationBinding, Store},
    Error, Result,
};
use rusqlite::{params, Connection};
use std::{path::Path, sync::Arc};

const ACCOUNT: &str = "00000000-0000-0000-0000-000000000001";

fn intent() -> Intent {
    Intent {
        network: "regtest".into(),
        wallet: "migration-fixture".into(),
        account: ACCOUNT.into(),
        recipient: Recipient {
            address: "uregtest1syntheticmigrationfixture".into(),
            amount_zat: 100_000,
            memo_hex: String::new(),
        },
        fee: FeePolicy {
            rule: "zip317".into(),
            max_fee_zat: 50_000,
        },
        expiry: ExpiryPolicy {
            min_height: 100,
            max_height: 200,
        },
        privacy: "FullPrivacy".into(),
    }
}

// Preserve the version-two shape rather than constructing a version-three DB
// and removing its protections to simulate an upgrade.
fn legacy_v2(path: &Path) -> Connection {
    let db = Connection::open(path).unwrap();
    db.execute_batch(
        "CREATE TABLE wallet(id INTEGER PRIMARY KEY CHECK(id=1),name TEXT NOT NULL,
           account TEXT NOT NULL,network TEXT NOT NULL CHECK(network='regtest'),payment TEXT);
         CREATE TABLE payments(id TEXT PRIMARY KEY,intent TEXT NOT NULL,
           stage TEXT NOT NULL DEFAULT 'pending' CHECK(stage IN ('pending','created','proven','signed','committed','accepted')),
           inflight TEXT CHECK(inflight IN ('create','prove','sign','extract')),
           owner TEXT,raw BLOB,byte_digest TEXT,txid TEXT,last_error TEXT,intent_digest TEXT,
           CHECK((raw IS NULL AND byte_digest IS NULL AND txid IS NULL) OR
                 (raw IS NOT NULL AND byte_digest IS NOT NULL AND txid IS NOT NULL)),
           CHECK((raw IS NULL AND stage NOT IN ('committed','accepted')) OR
                 (raw IS NOT NULL AND stage IN ('committed','accepted') AND inflight IS NULL)));
         CREATE TABLE artifacts(payment TEXT NOT NULL REFERENCES payments(id),stage TEXT NOT NULL,
           bytes BLOB NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(payment,stage));
         CREATE TABLE attempts(id INTEGER PRIMARY KEY,payment TEXT NOT NULL REFERENCES payments(id),
           byte_digest TEXT NOT NULL,outcome TEXT NOT NULL,detail TEXT,
           created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')));
         CREATE TABLE events(id INTEGER PRIMARY KEY,payment TEXT NOT NULL REFERENCES payments(id),
           kind TEXT NOT NULL,detail TEXT NOT NULL,
           created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')));
         PRAGMA user_version=2;",
    )
    .unwrap();
    db.execute(
        "INSERT INTO wallet(id,name,account,network) VALUES(1,?,?,'regtest')",
        params![intent().wallet, ACCOUNT],
    )
    .unwrap();
    db
}

fn insert_legacy(db: &Connection, id: &str, txid: Option<&str>) -> rusqlite::Result<usize> {
    let canonical = intent().canonical_json().unwrap();
    let raw = txid.map(|_| b"NOT-A-ZCASH-TRANSACTION:legacy".as_slice());
    db.execute(
        "INSERT INTO payments(id,intent,intent_digest,stage,raw,byte_digest,txid) VALUES(?,?,?,?,?,?,?)",
        params![
            id,
            canonical,
            digest(canonical.as_bytes()),
            if txid.is_some() { "committed" } else { "pending" },
            raw,
            raw.map(digest),
            txid
        ],
    )
}

fn fence_legacy_create(db: &Connection, id: &str) {
    db.execute(
        "UPDATE payments SET owner='legacy-owner',inflight='create' WHERE id=?",
        [id],
    )
    .unwrap();
    db.execute("UPDATE wallet SET payment=? WHERE id=1", [id])
        .unwrap();
}

fn legacy_rows(db: &Connection) -> Vec<Vec<Vec<rusqlite::types::Value>>> {
    ["wallet", "payments", "artifacts", "attempts", "events"]
        .into_iter()
        .map(|table| {
            let mut query = db
                .prepare(&format!("SELECT * FROM {table} ORDER BY rowid"))
                .unwrap();
            let columns = query.column_count();
            query
                .query_map([], |row| (0..columns).map(|index| row.get(index)).collect())
                .unwrap()
                .collect::<rusqlite::Result<Vec<_>>>()
                .unwrap()
        })
        .collect()
}

fn advance_to_extract(store: &Store, id: &str, owner: &str) {
    for (expected, operation, next) in [
        ("pending", "create", "created"),
        ("created", "prove", "proven"),
        ("proven", "sign", "signed"),
    ] {
        store.begin_stage(id, expected, operation, owner).unwrap();
        store
            .finish_stage(
                id,
                operation,
                next,
                owner,
                format!("NOT-A-PCZT:{id}:{next}").as_bytes(),
            )
            .unwrap();
    }
    store.begin_stage(id, "signed", "extract", owner).unwrap();
}

#[test]
fn schema_two_upgrade_enforces_case_insensitive_txids_and_preserves_legacy_fences() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("legacy-v2.db");
    let upper = "A1".repeat(32);
    let db = legacy_v2(&path);
    insert_legacy(&db, "existing", Some(&upper)).unwrap();
    insert_legacy(&db, "legacy-pending", None).unwrap();
    insert_legacy(&db, "another-pending", None).unwrap();
    fence_legacy_create(&db, "legacy-pending");
    drop(db);

    let store = Store::open(&path, &intent().wallet, ACCOUNT).unwrap();
    assert_eq!(
        store.get("existing").unwrap().txid.as_deref(),
        Some(upper.as_str())
    );
    assert_eq!(
        store.committed_bytes("existing").unwrap(),
        b"NOT-A-ZCASH-TRANSACTION:legacy"
    );
    let pending = store.get("legacy-pending").unwrap();
    assert_eq!(pending.owner.as_deref(), Some("legacy-owner"));
    assert_eq!(pending.inflight.as_deref(), Some("create"));
    let db = store.connect().unwrap();
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        3
    );
    assert_eq!(
        db.query_row("SELECT payment FROM wallet WHERE id=1", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        "legacy-pending"
    );
    assert!(insert_legacy(&db, "duplicate-case", Some(&upper.to_ascii_lowercase())).is_err());
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM payments WHERE txid IS NULL",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        2
    );
}

#[test]
fn duplicate_legacy_txids_refuse_migration_without_choosing_a_winner() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("duplicate-v2.db");
    let db = legacy_v2(&path);
    insert_legacy(&db, "first", Some(&"ab".repeat(32))).unwrap();
    insert_legacy(&db, "second", Some(&"AB".repeat(32))).unwrap();
    insert_legacy(&db, "legacy-pending", None).unwrap();
    fence_legacy_create(&db, "legacy-pending");
    let before = legacy_rows(&db);
    drop(db);

    assert!(matches!(
        Store::open(&path, &intent().wallet, ACCOUNT),
        Err(Error::Reconciliation(_))
    ));
    let db = Connection::open(&path).unwrap();
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        2
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM payments", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        3
    );
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM payments WHERE lower(txid)=?",
            ["ab".repeat(32)],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        2
    );
    assert_eq!(
        db.query_row(
            "SELECT owner || ':' || inflight FROM payments WHERE id='legacy-pending'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "legacy-owner:create"
    );
    assert_eq!(
        db.query_row("SELECT payment FROM wallet WHERE id=1", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        "legacy-pending"
    );
    assert_eq!(db.query_row("SELECT count(*) FROM sqlite_master WHERE name IN ('create_operations','payments_txid_unique')", [], |r| r.get::<_, i64>(0)).unwrap(), 0);
    assert_eq!(legacy_rows(&db), before);
}

#[test]
fn duplicate_final_txid_rolls_back_bytes_and_keeps_extraction_ownership() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path().join("outbox.db"), &intent().wallet, ACCOUNT).unwrap();
    store.submit("first", intent()).unwrap();
    store.submit("second", intent()).unwrap();
    advance_to_extract(&store, "first", "owner-one");
    let txid = "A2".repeat(32);
    store
        .commit_final(
            "first",
            "owner-one",
            b"NOT-A-ZCASH-TRANSACTION:first",
            &txid,
        )
        .unwrap();
    assert_eq!(
        store.get("first").unwrap().txid.as_deref(),
        Some(txid.to_ascii_lowercase().as_str())
    );
    advance_to_extract(&store, "second", "owner-two");
    assert!(matches!(
        store.commit_final(
            "second",
            "owner-two",
            b"NOT-A-ZCASH-TRANSACTION:second",
            &txid.to_ascii_lowercase()
        ),
        Err(Error::Sqlite(_))
    ));
    let second = store.get("second").unwrap();
    assert!(second.txid.is_none());
    assert_eq!(second.stage, "signed");
    assert_eq!(second.owner.as_deref(), Some("owner-two"));
    assert_eq!(second.inflight.as_deref(), Some("extract"));
    assert_eq!(second.submission_attempts, 0);
    assert_eq!(
        store.committed_bytes("first").unwrap(),
        b"NOT-A-ZCASH-TRANSACTION:first"
    );
    let db = store.connect().unwrap();
    assert_eq!(
        db.query_row("SELECT payment FROM wallet WHERE id=1", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        "second"
    );
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM artifacts WHERE payment='second'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        3
    );
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM events WHERE payment='second' AND kind='final_bytes_committed'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
}

#[test]
fn legacy_inflight_create_without_original_binding_blocks_before_adapter_or_response_read() {
    struct NeverCalled;
    impl Wallet for NeverCalled {
        fn qualified(&self) -> Result<()> {
            panic!("legacy recovery must not call the adapter")
        }
        fn create(&self, _: &Intent, _: &OperationBinding) -> Result<Vec<u8>> {
            panic!("no CREATE replay")
        }
        fn prove(&self, _: &[u8]) -> Result<Vec<u8>> {
            panic!("no proving")
        }
        fn sign(&self, _: &[u8], _: &Intent) -> Result<Vec<u8>> {
            panic!("no signing")
        }
        fn inspect(&self, _: &[u8], _: &Intent) -> Result<()> {
            panic!("no inspection")
        }
        fn extract_verified(&self, _: &[u8], _: &Intent) -> Result<(Vec<u8>, String)> {
            panic!("no extraction")
        }
        fn recover_created(&self, _: &Path, _: &Intent, _: &OperationBinding) -> Result<Vec<u8>> {
            panic!("unbound evidence must not reach the adapter")
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("legacy-v2.db");
    let db = legacy_v2(&path);
    insert_legacy(&db, "legacy-pending", None).unwrap();
    fence_legacy_create(&db, "legacy-pending");
    drop(db);
    let store = Store::open(&path, &intent().wallet, ACCOUNT).unwrap();
    let engine = Engine::new(
        store.clone(),
        Arc::new(NeverCalled),
        Arc::new(PublicationDisabled),
    );
    assert!(matches!(
        engine.reconcile_create(
            "legacy-pending",
            &dir.path().join("nonexistent-response.json")
        ),
        Err(Error::Reconciliation(_))
    ));
    let payment = store.get("legacy-pending").unwrap();
    assert_eq!(payment.owner.as_deref(), Some("legacy-owner"));
    assert_eq!(payment.inflight.as_deref(), Some("create"));
    assert!(payment.txid.is_none());
    let db = store.connect().unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM create_operations", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        db.query_row("SELECT payment FROM wallet WHERE id=1", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        "legacy-pending"
    );
}
