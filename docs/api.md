# HTTP API and CLI contract for 0.1.0

The service binds one durable SQLite database to one dedicated wallet/account on regtest. Registration and driving a payment are separate operations. HTTP 202 acknowledges durable intent registration. It does not acknowledge payment.

Without `--live-config`, both wallet construction and publication are disabled, including publication from existing committed records. The explicit live configuration selects only the pinned disposable NU6.3/V6/Ironwood profile described in [wallet qualification](qualification-wallet.md). Health describes configuration, not wallet liveness or a verified restore.

## Authentication and transport

All routes require `Authorization: Bearer <token>`. `serve --token-file PATH` reads 32–256 printable ASCII characters after trimming. The API accepts only numeric loopback listeners and returns JSON with `Cache-Control: no-store`. RPCs use numeric HTTP loopback origins, no environment proxies, and no redirects. Protect tokens, SQLite/WAL files, intents, viewing material, PCZT artifacts and evidence directories. CLI access uses host filesystem permissions.

## Immutable intent

`PUT /payments/{id}` accepts one object with these required fields; unknown and duplicate typed fields are rejected. IDs and wallet names contain 1–128 ASCII letters, digits, `_`, `-`, `:`, or `.`. Paths are literal and have no trailing slash or query string.

| Field | Constraint |
|---|---|
| `network` | Exactly `regtest` |
| `wallet`, `account` | Dedicated wallet name and account UUID; UUID canonicalized |
| `recipient.address` | Regtest UA; registration checks syntax, live verification decodes and checks the actual Orchard receiver |
| `recipient.amount_zat` | Positive integer zatoshis, at most 2,100,000,000,000,000 |
| `recipient.memo_hex` | Whole hex bytes, at most 512 bytes, normalized lowercase; empty means canonical no-memo (`f6` plus padding), nonempty bytes are zero padded by the wallet |
| `fee.rule` | Exactly `zip317` |
| `fee.max_fee_zat` | Positive integer ceiling; amount plus ceiling cannot exceed MAX_MONEY |
| `expiry.min_height`, `expiry.max_height` | Approved inclusive interval for the chosen transaction's expiry; minimum at least 1, maximum u32, ordered |
| `privacy` | Exactly `FullPrivacy` |

**Supported shape: exactly one positive sender input and one positive recipient output**, with two through eight Ironwood actions. The verifier checks every change output under the trusted internal viewing key, all 512 memo bytes, exact ZIP-317 fee, branch, expiry, proofs and signatures. A valid wallet transaction with multiple positive inputs is unsupported and may quarantine the dedicated wallet after CREATE. Unsupported shapes fail closed; an error does not authorize ownership release. `examples/intent.json` is a synthetic registration fixture and cannot be spent to.

Canonical identity ignores object key order and JSON whitespace, normalizes UUID and memo hex case, and otherwise compares all typed fields exactly. The first request persists canonical intent and its SHA-256 digest. Equivalent requests return the existing record. A valid changed intent under the same ID returns 409; invalid data may return 400 before comparison. A new ID is a distinct payment, even if the recipient and amount match.

## Routes

| Method/path | Success | Behavior |
|---|---|---|
| `GET /health` | 200 | `profile_qualified`, `publication_enabled`, `wallet_availability: not_probed`, and state `qualification_blocked` or `disposable_regtest`; describes configured mode |
| `PUT /payments/{id}` | 202 | Register or return one immutable payment; never implicitly drive a stage |
| `GET /payments/{id}` | 200 | Return payment, checking committed-byte and intent integrity |
| `GET /payments` | 200 | Return all retained records in insertion order, including accepted/confirmed records for reconciliation |
| `POST /payments/{id}/retry` | 200 | Drive an eligible payment; return accepted records without another construction/submission |
| `GET /payments/{id}/evidence` | 200 | Payment, nullable original CREATE operation binding, artifact stage/size/digest, attempts, events; excludes raw PCZT bodies and credentials |
| `POST /payments/{id}/refresh` | 200 | Observe real chain state; optional body `{"confirmations":3}`, depth 1–100; default 3 |

PUT is limited to 64 KiB; refresh is limited to 1 KiB. Other POST bodies are unused. No raw import, cancellation, replacement, owner release, timeout takeover, or force-resume endpoint exists.

A `Payment` contains `id`, canonical `intent`, persisted `intent_digest`, `stage`, `inflight`, `owner`, `txid`, `byte_digest`, `last_error`, `submission_attempts`, and nullable `chain_observation`. Stages are `pending`, `created`, `proven`, `signed`, `committed`, and `accepted`.

`accepted` records a node receipt, not business settlement. Refresh records `unknown`, `node_observed`, `mined`, or `confirmed`, with observed confirmation count, required depth, block hash, tip and txid. A lookup failure is uncertainty. `prior_mining_no_longer_observed` flags loss of a previous observation; `mining_reversed` requires a successful current lookup showing that previous block is no longer the transaction's block. Confirmation observations can become stale and require refresh. They are not irreversible finality.

Every postcommit retry reads and checks the stored bytes, checks their actual V6 expiry/header against the approved interval, and submits those same bytes. It bypasses all wallet operations. No accepted result or chain observation permits reconstruction. A precommit uncertain RPC or failed result COMMIT preserves its owner and wallet-wide slot and returns reconciliation required. Restart does not clear those fences. A completed artifact with no in-flight marker can resume under its existing owner through the stage CAS.

Before CREATE, the same SQLite transaction that claims the wallet saves an immutable `create_operations` record containing `operation_id`, `payment_id`, `owner`, and `intent_digest`. The adapter saves this exact binding in `create-{operation_id}/binding.json` before calling CREATE. Operator recovery requires the original database binding, exact evidence binding and operation directory, plus all existing provenance/effects checks; matching payment details alone are insufficient. Schema migration does not generate bindings for historical operations. Unbound evidence stays quarantined.

## Errors

Errors are `{"error":"diagnostic text"}`. Retain the same business ID on every error.

| Status | Meaning |
|---|---|
| 400 | Invalid schema, ID, identity, numeric bounds or body limit |
| 401 | Missing/incorrect bearer token |
| 404 | Missing payment, path or supported method |
| 409 | Changed intent, busy wallet, uncertain operation, expired transaction or reconciliation required |
| 500 | Integrity, SQLite COMMIT, I/O or other internal failure |
| 502 | Remote call failed or its outcome is uncertain |
| 503 | Default publication gate, unsupported profile or independent verifier rejection |

Remote diagnostics preserve method/code, not arbitrary secret-bearing server text. A transport failure does not establish cancellation. The evidence view is read-only and does not authorize recovery.

## CLI

Global options precede the subcommand:

```text
zcash-durable-outbox [--db PATH] [--wallet NAME] [--account UUID]
  [--zebra-url http://LOOPBACK:PORT] [--live-config PATH] COMMAND
```

Commands: `reconcile-create ID --response PATH` (operator-only, qualified saved CREATE provenance; see [recovery](recovery.md)), `init`, `submit ID --intent PATH`, `get ID`, `unresolved`, `retry ID`, `evidence ID`, `refresh ID --confirmations 3`, and `serve --token-file PATH --listen 127.0.0.1:17840`. `probe-wallet --url URL --cookie-file PATH` performs only a read-only wallet `getinfo`.

The live config has exactly six fields: `wallet_url`, `node_url`, `credential_file`, `trusted_wallet`, `verifier`, and `evidence_dir`. See [example configuration](../examples/live-config.example.json). Use absolute private paths. The credential file contains synthetic `user:password`. The viewing registration schema is documented in [verifier/README](../verifier/README.md). Construct it from the newly provisioned dedicated account, never from a PCZT. Both wallets must run with `external.broadcast=false`; no unrelated wallet caller may construct or publish.

SQLite schema 3 migrates schemas 1 and 2 transactionally, backfills canonical intent digests where needed, adds immutable CREATE bindings for future operations, and enforces a unique case-insensitive index on non-null transaction IDs. A legacy database with duplicate transaction IDs refuses migration/startup while retaining every payment, owner, artifact and schema version; operators must investigate the conflicting business records. Migration never chooses a winner, deletes a payment or clears a fence. A duplicate final commit also rolls back and retains the losing payment's extraction fence, without publication. This safeguard supplements the operation binding.

The live mode is for fresh disposable regtest experiments. Verified backup/restore and freshness enforcement, deployment enforcement of the wallet contract, NU7, actual expiry/rejection and broader recovery remain operational release gates; see [backup/restore](backup-restore.md) and [acceptance status](acceptance-status.md).

