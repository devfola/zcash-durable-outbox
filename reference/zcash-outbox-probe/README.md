# Durable payment outbox: local feasibility probe

**Research prototype. Not production code, a Zcash wallet, an RPC adapter, or a payment service.**

This dependency-free Python experiment asks whether a single-host, dedicated-wallet payout worker can durably bind a business payment intent, persist its chosen signed bytes, and retry a lost broadcast response without constructing another payment. It uses SQLite, separate child processes, and explicitly fake wallet/broadcaster implementations. It never contacts a network, uses real addresses or keys, installs software, or sends money.

## Run

Requires Python 3.10+ with its standard-library SQLite module. Verified here with Python 3.12.14 and SQLite 3.53.1.

```sh
python3 probe.py test --json results.json
```

The suite creates disposable databases in a local temporary directory. Only the optional JSON result is retained. Expect approximately 6 seconds on the tested environment. `worker` is an internal fault-injection CLI used by the tests, not a service interface.

## Result

The recorded run passed **15 test groups**, including **35 durability-boundary process-crash cases**, with zero failures. Additional cases cover ambiguous operation outcomes, concurrency, actual SQLite COMMIT rejection, and an adverse baseline.

For a single business intent, the safe model accepted one fixture payment, and every broadcast attempt used the exact durable bytes. The deliberately unsafe send-first/save-later baseline accepted **two fixture payments**, spending two unrelated synthetic notes after a crash between sending and saving the receipt.

That baseline is our own counterexample. It is **not a runtime reproduction of zec-ui, Zallet, or any third-party application**.

## What is modeled

1. **Durable intent identity.** An immutable intent ID binds canonical JSON containing the fake network, recipients, integer base-unit amounts, memos, fee, and expiry height. Reordered object keys compare equal. Reusing an ID with changed payload or adapter policy is rejected. This is a narrow fixture schema, not real address or protocol validation. Recipient array order remains significant.
2. **Atomic per-intent claim.** `BEGIN IMMEDIATE` serializes claim decisions. Concurrent retries cannot each start a payment build. An unresolved live-looking claim is not stolen by timeout.
3. **Wallet-wide build serialization.** A second durable lock covers create→extract for the entire dedicated fake wallet. Draft creation deliberately does not reserve notes; extraction does. Two different intent IDs therefore cannot build from the same unreserved note at once. A pre-broadcast retry retains that wallet slot for the original intent. Unknown-outcome halts retain it too, deliberately sacrificing availability pending reconciliation.
4. **Durable stage inputs.** The last completed create/prove/sign output is committed before the next stage starts. A committed in-flight marker identifies ambiguous outcomes. The model can retry a stage from that exact durable input; it does not restart a completed pipeline from a fresh create.
5. **Persist before publish.** Signed raw fixture bytes and their byte hash are committed together before the broadcaster is called. The broadcaster obtains bytes from a fresh database read; it never receives an uncommitted wallet return value. A real SQLite deferred-foreign-key COMMIT failure rolls the raw bytes back and produces zero broadcast attempts.
6. **Byte-identical broadcast retry.** Response loss or process death after the broadcaster accepts a transaction does not trigger create/prove/sign/extract again. Recovery reloads the saved bytes. This is the critical outbox boundary.
7. **Fail-closed ambiguity.** An unknown RPC contract halts on lost pre-broadcast responses or uncertain in-flight crash outcomes. A stored-byte integrity mismatch also halts. A supplied expiry/reorg ambiguity flag stops further sends or automatic replacements while retaining evidence.

### The two pre-broadcast recovery policies

- `no_broadcast`: an explicit adapter assumption that create, prove, sign, and extract cannot broadcast, and that the worker is the sole publisher of their outputs. Lost responses may be retried before raw-byte commitment. Create can be retried from the durable intent before any completed draft exists. Later stages retry the exact last committed artifact. The fake operations intentionally return different nonce-bearing bytes on a retry, including extraction; no abandoned output can reach the broadcaster.
- `unknown`: when those guarantees are unavailable, the worker safely stops instead of rebuilding blindly. “HALTED” means reconciliation is required by this toy model, not that a real system must remain permanently stopped or require a person for every timeout.

The first policy is an **assumption to qualify against the exact selected Zallet release and deployment**, not a guarantee proved by this script. In particular, do not generalize a no-broadcast property from one RPC/version to other wallet commands, background workers, or plugins. Separate source review can justify an adapter design; this probe does not replace integration testing.

Even when pre-commit re-extraction is permitted, **after raw-byte commitment, always reload the committed bytes. Never re-extract as a broadcast retry.**

## State and durability boundaries

The main path is:

```text
PENDING → BUILDING → READY → SENDING → ACK
             ↓                   ↓
           HALTED               READY (lost send response)
```

Each build stage persists its in-flight fence, runs the fake operation, and commits the output. `READY` includes raw bytes, a byte-integrity hash, and completion of the wallet-wide build slot. The extraction reservation and outbox write intentionally live in separate databases, so there is no imaginary transaction spanning wallet and application.

`ACK` means only a fake broadcaster acceptance receipt. It is **not confirmation, finality, or fulfilled business settlement**. Reorg handling can transition even an acknowledged intent to `HALTED` for reconciliation.

The 35 boundary cases are:

- Intent insertion: immediately before COMMIT and immediately after COMMIT
- Worker claim: before claim and after committed claim
- Each of create/prove/sign/extract: before its fence, after committed fence, after fake operation completion but before its response, after response, before output COMMIT, and after output COMMIT
- Broadcast: before send claim, after committed send claim, before call, after fake acceptance but before response, and after response
- Receipt persistence: before COMMIT and after COMMIT

The tests use `os._exit(91)` in actual child processes. This bypasses normal Python cleanup. The parent waits for child death, then restarts work against the same SQLite files. The database uses WAL and `synchronous=FULL`.

`recover_after_confirmed_worker_death` is a **supervisor-only test operation**. It is not a distributed lease mechanism. An ordinary retry returns `BUSY` for unresolved ownership rather than taking over. Real remote RPCs may continue after their caller dies; that behavior is not simulated or qualified here.

## Test inventory

- Canonical payload binding; rejection of changed amount, fee, or expiry; rejection of float, Boolean, and negative amounts
- Intent persistence before/after COMMIT and retry after a lost acknowledgment
- Ordinary execution and completed-request retry
- Twelve concurrent same-ID submission/claim callers
- Two distinct intents sharing a wallet with unreserved draft inputs
- Wallet-wide slot retention across a lost prove response
- Twelve concurrent retries following broadcast acceptance with a lost response
- Thirty-three build/send/receipt crash boundaries, plus two intent boundaries
- Lost create/prove/sign/extract responses under the explicit non-broadcasting assumption, replaying identical stage inputs despite different outputs
- Four lost-response and four process-crash cases under unknown RPC semantics; all halt with zero broadcast attempts
- Real deferred-constraint failure at the raw-byte COMMIT; no signed payload reaches the broadcaster
- Expiry/reorg stop conditions, including reorg ambiguity after an acceptance receipt
- Stored raw-byte tampering fails closed
- An ordinary retry cannot steal an unresolved build claim
- Unsafe send-first/save-later duplicate-payment counterexample

`results.json` contains the exact observed group results, crash-point names, and relevant counts.

## What is not established

- **No real Zcash integration.** There are no PCZTs, proofs, signatures, keys, nullifiers, consensus transactions, Zallet processes, Zebra processes, RPC calls, or transaction broadcasts. The fake nonce fields deliberately stand in for potentially variable output bytes.
- **No Zcash transaction-ID implementation.** SHA-256 here names fixture bytes. It does not model Zcash v5 transaction IDs, authorizing-data commitments, or the semantics of changed binding signatures.
- **No global exactly-once delivery or settlement claim.** Another application can create a new intent ID for the same invoice, publish an abandoned output, independently use the wallet, restore an old database backup, or run a replacement policy. Those cases are outside the invariant. Network acceptance alone also does not establish a recipient's final payment.
- **No distributed/HA fencing.** One SQLite database and one cooperative dedicated-wallet pipeline are assumed. Multiple hosts, external wallet users, asynchronous RPC completion after caller death, lease expiry, partitions, and stale workers require further qualification or design.
- **No physical durability proof.** Process death and a logical SQLite COMMIT rejection were tested. Disk-full, fsync errors, controller caches, kernel crashes, power loss, filesystem corruption, interrupted checkpointing, backup restoration, and ambiguous commit outcomes were not.
- **No automatic chain reconciliation.** Height/reorg flags are supplied by the test. Actual expiry rules, live chain-height changes during building/sending, reorg depth, mempool eviction, confirmations, rejection classification, and safe transaction replacement are not implemented.
- **No operational/security hardening.** No credential management, encryption, access control, journal retention, audit tamper resistance, metrics, migrations, cancellation, note accounting, fee selection, address/memo validation, throughput capacity, or operator recovery interface is provided.

## Smallest next qualification step

Keep the same state machine and fault-injection contract, but build a separately reviewed adapter against pinned wallet/node versions in an isolated test environment. Verify, rather than assume:

- create/prove/sign/extract cannot broadcast or schedule background rebroadcast
- actual extraction success/storage semantics, including retries after a lost result
- how note reservations and completed artifacts behave across wallet and application restarts
- exact returned raw bytes can be independently stored and then submitted to the node's raw-transaction broadcast API
- an accepted-but-lost broadcast response is handled by rebroadcasting those same bytes
- real expired, conflicting, rejected, and reorged transactions remain unresolved until a safe reconciliation policy supplies evidence

This probe supports the feasibility of the **application durability pattern under stated assumptions**. It does not establish production readiness or a grant-ready integration proof.
