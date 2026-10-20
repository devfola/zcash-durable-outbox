CREATE TABLE IF NOT EXISTS wallet (
  id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL, account TEXT NOT NULL,
  network TEXT NOT NULL CHECK(network='regtest'), payment TEXT
);
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY, intent TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'pending' CHECK(stage IN ('pending','created','proven','signed','committed','accepted')),
  inflight TEXT CHECK(inflight IN ('create','prove','sign','extract')),
  owner TEXT, raw BLOB, byte_digest TEXT, txid TEXT, last_error TEXT,
  CHECK ((raw IS NULL AND byte_digest IS NULL AND txid IS NULL) OR
         (raw IS NOT NULL AND byte_digest IS NOT NULL AND txid IS NOT NULL)),
  CHECK ((raw IS NULL AND stage NOT IN ('committed','accepted')) OR
         (raw IS NOT NULL AND stage IN ('committed','accepted') AND inflight IS NULL))
);
CREATE TABLE IF NOT EXISTS artifacts (
  payment TEXT NOT NULL REFERENCES payments(id), stage TEXT NOT NULL,
  bytes BLOB NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(payment,stage)
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY, payment TEXT NOT NULL REFERENCES payments(id),
  byte_digest TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, payment TEXT NOT NULL REFERENCES payments(id),
  kind TEXT NOT NULL, detail TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- No legacy binding is synthesized. A pre-migration in-flight CREATE remains fenced.
CREATE TABLE IF NOT EXISTS create_operations (
  operation_id TEXT PRIMARY KEY NOT NULL,
  payment_id TEXT NOT NULL UNIQUE REFERENCES payments(id),
  owner TEXT NOT NULL, intent_digest TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TRIGGER IF NOT EXISTS create_operation_immutable BEFORE UPDATE ON create_operations
BEGIN SELECT RAISE(ABORT,'immutable original CREATE binding'); END;
CREATE TRIGGER IF NOT EXISTS create_operation_no_delete BEFORE DELETE ON create_operations
BEGIN SELECT RAISE(ABORT,'original CREATE binding cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS intent_immutable BEFORE UPDATE OF intent,id ON payments
BEGIN SELECT RAISE(ABORT,'immutable payment identity'); END;
CREATE TRIGGER IF NOT EXISTS final_immutable BEFORE UPDATE OF raw,byte_digest,txid ON payments
WHEN OLD.raw IS NOT NULL
BEGIN SELECT RAISE(ABORT,'immutable final transaction'); END;
CREATE TRIGGER IF NOT EXISTS artifact_immutable BEFORE UPDATE ON artifacts
BEGIN SELECT RAISE(ABORT,'immutable completed artifact'); END;
CREATE TRIGGER IF NOT EXISTS artifact_no_delete BEFORE DELETE ON artifacts
BEGIN SELECT RAISE(ABORT,'completed artifacts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS payment_no_delete BEFORE DELETE ON payments
BEGIN SELECT RAISE(ABORT,'payment identities cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS wallet_identity_immutable BEFORE UPDATE OF name,account,network ON wallet
BEGIN SELECT RAISE(ABORT,'immutable wallet identity'); END;
