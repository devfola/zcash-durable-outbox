# Zcash Durable Payment Outbox

A Rust/SQLite payment outbox with a CLI, authenticated loopback HTTP API, actual Zallet PCZT adapter, independent transaction verifier, and durable bounty/refund reference application. It saves approved payment identity and artifacts, commits chosen transaction bytes before submission, and reuses those bytes after commitment.

**Saved-CREATE origin binding is corrected and verified on the fresh real stack.** The pre-correction recovery lacked a durable binding to the originating payment and operation. Matching transaction details did not prove that a response belonged to that payment: two IDs with identical recipient/amount/memo could cross-adopt different operations. The missing check was confirmed; no on-chain loss was demonstrated. Corrected local validation passed: **43 Rust tests (42 library + 1 CLI)**, formatting, strict Clippy with warnings denied, build, and **10 compiled HTTP/CLI smoke checks with zero remote calls**. [Correction results and hashes](evidence/correction/summary.json). The corrected executable subsequently passed all nine scenarios plus genuine identical-intent A/B CREATE recovery on a fresh disposable stack with the actual verifier. [Corrected real-stack report](docs/corrected-real-stack-acceptance.md).

The earlier nine-scenario run, reorg and unbound saved-CREATE supplements are **historical qualification of the recorded pre-correction binary**. They do not qualify the corrected executable or test cross-payment operation binding. Their hashes and results remain preserved. [Historical suite](integration/evidence/real-stack-final/summary.json), [historical tested hashes](integration/evidence/real-stack-final/tested-artifacts.json), [historical reorg](integration/evidence/controlled-reorg/summary.json), [current acceptance scope](docs/acceptance-status.md).

**Exactly one positive Ironwood sender input is supported.** Multiple positive inputs and other unsupported shapes are rejected. A rejection after a wallet operation begins—including an ordinary-looking verification or RPC error—can leave the entire dedicated wallet quarantined. The failure path retains ownership and its in-flight fence; it does not clear them, retry under a new ID, or move the wallet to a fresh database.

**Default mode disables wallet construction and publication.** `--live-config` explicitly enables one bounded profile for a freshly provisioned, disposable regtest wallet/outbox pair. This is a reproducible prototype, not a production or public-network release. NU7 compatibility, verified backup/restore, and general recovery of uncertain precommit wallet operations remain release gates.

Actual expiry/rejection qualification and independent external review also remain release work. A controlled invalidation receipt supports the reorg experiment; an unknown transaction lookup alone is never treated as proof of reversal.

Actual checksum-verified Zallet/Zebra processes have funded sender/receiver wallets, completed the shielded PCZT pipeline, and verified receiver payments. Repeated extraction of the same signed PCZT produced the same independently computed transaction ID with **different serialized bytes**. Caller-death experiments also observed wallet completion after the caller disappeared. These findings are why extraction is forbidden after final-byte commitment and why an uncertain earlier operation keeps its ownership fence. See [wallet qualification](docs/qualification-wallet.md), [current service acceptance](docs/acceptance-status.md), and the [corrected service evidence](integration/evidence/real-stack-corrected-001/). Individual reports and the final acceptance report define the tested scope; infrastructure checks are labeled separately.

## Supported disposable profile

| Component | Exact selected runtime |
|---|---|
| Zallet | Official `v0.1.0-beta.3`, source `987382f67e622915228686e9f956c6a9c9a7514c`, `zallet-zaino` backend |
| Zebra | Official `v6.2.3`, source `7121c82ba6795a151523d5b308a19743f4a1ade7` |
| Wallet/verifier dependency | librustzcash `1f6bb2072e7fcb142b0d90ff7b267a8699a84818`, Orchard `0.15.3`; retain both checked-in Cargo lockfiles |
| Application/helper toolchain | Rust `1.95.0`; native C/C++ toolchain for dependencies |
| Network and transaction | Regtest, NU6.3 branch `37a5165b`, V6, Ironwood only |
| Payment shape | One dedicated wallet/account, one recipient, **exactly one positive sender input**, two through eight actions, individually verified internal change |

Earlier upgrades activate at height 1 and NU5 through NU6.3 at height 2. The adapter checks the exact activation map and regtest genesis, not only Zebra's shared BIP70 `chain: test` label. [Pinned sources and archive digests](integration/pinned-sources.json), [stack bootstrap/runbook](docs/qualification-stack.md), [verifier design](docs/payment-verifier-design.md), and [NU7 assessment](docs/qualification-compatibility.md) contain the complete profile. An earlier source inspection used later Zallet HEAD `e5c16c4...`; it is distinguished from the executed release, whose six relevant PCZT files match that inspection.

The live verifier independently checks recipient receiver, integer value, all 512 memo bytes, trusted change ownership, fee, expiry, branch, proofs and authorization. It parses the extracted consensus transaction, derives its actual transaction ID, and checks its bytes against the signed artifact. Zallet's `pczt_inspect` response alone cannot authorize payment. The standalone helper has no RPC or publisher; the Rust adapter calls it against saved artifacts. [Actual verifier validation](verifier/VALIDATION.md).

## Build and default local mode

Use Rust 1.95.0 and a native compiler. On Windows use a Visual Studio Developer PowerShell. Run from this project directory:

```sh
cargo +1.95.0 build --locked
cargo +1.95.0 test --locked
cargo +1.95.0 build --locked --release --manifest-path verifier/Cargo.toml
cargo +1.95.0 run --locked -- init
cargo +1.95.0 run --locked -- submit bounty:example --intent examples/intent.json
cargo +1.95.0 run --locked -- get bounty:example
```

`examples/intent.json` contains a synthetic registration address. It cannot be used for a live payment. Registration returns a durable pending record; it does not construct or submit a transaction.

Create a fresh local bearer token without printing it, then start the API:

```sh
python -c "import pathlib,secrets; pathlib.Path('runtime').mkdir(exist_ok=True); pathlib.Path('runtime/api-token').open('x').write(secrets.token_hex(32))"
cargo +1.95.0 run --locked -- serve --token-file runtime/api-token
```

All routes require the bearer token. The listener defaults to numeric loopback `127.0.0.1:17840`; RPC transport also requires numeric HTTP loopback, ignores proxies, and refuses redirects. `GET /health` describes configuration, not wallet availability or restore verification.

## Enable the disposable live adapter

Provision a **new dedicated disposable wallet/outbox pair** using the [stack runbook](docs/qualification-stack.md). The tested Windows route uses a workspace-local Linux VM with existing WHPX acceleration, four vCPUs and 6144 MiB RAM; it required no host installer or administrator change. Native x86_64 Linux can use the official release bootstrap directly.

The packaged fresh-root setup has also completed using the previously verified official binaries in the existing VM: it gracefully stopped the old processes, preserved their state/claims, and funded a separate chain at setup height 107. The actual registration indices were sender `1` and receiver `4609798954`; both were taken from matching wallet records, not assumed zero. This does not claim a new host download or VM install. [Fresh setup metadata](integration/evidence/fresh-runtime-funding.json).

Before any business PCZT, finish isolated synthetic funding, stop the sender, set `external.broadcast=false`, restart it, and bind its trusted viewing registration to the dedicated account. Keep both wallets' publication disabled. **The adapter assumes this provisioned setting and exclusive wallet access; it does not independently attest the wallet's broadcast configuration or exclude other RPC clients.** Only the Outbox should publish its chosen committed bytes.

Copy [live-config.example.json](examples/live-config.example.json) to a protected private location and replace every path. It specifies `wallet_url`, `node_url`, `credential_file` containing `user:password`, `trusted_wallet` containing the private account/viewing registration, the compiled verifier executable, and a private `evidence_dir`. Use absolute paths. The verifier executable is named `outbox-artifact-verifier-disabled` (add `.exe` on Windows); its name reflects its standalone lack of publication capability. Never include credentials, viewing keys or complete PCZTs in a public evidence package.

With a real approved recipient intent for that environment:

```sh
cargo +1.95.0 run --locked -- --db runtime/outbox.db --wallet disposable-sender --account <sender-account-uuid> --live-config runtime/live-config.json submit refund:purchase-42 --intent runtime/approved-refund.json
cargo +1.95.0 run --locked -- --db runtime/outbox.db --wallet disposable-sender --account <sender-account-uuid> --live-config runtime/live-config.json retry refund:purchase-42
cargo +1.95.0 run --locked -- --db runtime/outbox.db --wallet disposable-sender --account <sender-account-uuid> get refund:purchase-42
cargo +1.95.0 run --locked -- --db runtime/outbox.db --wallet disposable-sender --account <sender-account-uuid> evidence refund:purchase-42
```

Keep the same database, wallet and account options on every invocation. Start `serve --token-file ...` with those same database/identity/live-config options for the HTTP workflow. [API contract](docs/api.md) covers `submit`, `get`, `unresolved`, `retry`, `evidence`, and `refresh --confirmations N` plus their HTTP routes. [Reference application](docs/reference-app.md) covers its separate durable business ledger and `register/query/pay/refresh` commands.

## Durability and operating limits

The first request binds canonical intent and its SHA-256 digest to an immutable ID. SQLite uses WAL, `synchronous=FULL`, immediate write transactions, and identity/artifact/final-byte constraints. Every wallet stage records a durable owner and in-flight marker before its RPC; completed artifacts are saved before the next stage. A timeout, process death, rejected stage COMMIT, or missing response retains the wallet-wide fence. There is no lease takeover or force-resume command.

Saved-CREATE recovery requires a durable operation ID bound **before the Zallet CREATE call** to the payment ID, existing owner and canonical-intent digest. Recovery must exactly match that tuple in the database and saved evidence. Similar request contents, matching effects, a directory name or an artifact hash alone do not establish origin. **Legacy evidence without this binding stays quarantined.** `reconcile-create` must neither invent a missing binding nor repeat CREATE. See the [corrected recovery contract and regression scope](docs/recovery.md). The historical same-owner supplement did not test cross-operation provenance.

A local rejection proven to occur before any operation begins is distinct from a failure after a durable in-flight marker or RPC attempt. Error text alone cannot prove rollback or cancellation. No generic ordinary-error cleanup releases ownership. The implemented case-insensitive unique non-null txid constraint and safe duplicate-data migration are additional locally tested database safeguards, not substitutes for the CREATE binding.

Final bytes, their integrity digest, independently derived consensus txid, extracted artifact, and wallet-slot release share one SQLite transaction. Submission records an attempt before handing those bytes to Zebra. Eligible postcommit retries bypass create/prove/sign/extract and use only the committed bytes. An actual acceptance receipt is separate from mining and business settlement. Chain refresh records unknown, node-observed, mined or confirmed state at a chosen depth, including reversal of prior mining observations; it never authorizes replacement construction. The business application reports paid only from a matching confirmed observation meeting its stored threshold.

Zallet and Outbox use separate databases: an extraction may have committed in the wallet even when its response or the Outbox COMMIT is lost. Retained artifacts and ownership do not prove rollback. General precommit reconciliation remains unqualified; terminal fault tests quarantine affected disposable wallets rather than clearing claims.

**There is no enforced verified-restore gate.** Default mode blocks sending, but explicit live configuration can enable publication of existing valid committed records. It does not detect a stale, internally valid database or wallet/outbox snapshot skew. Use live mode only with the freshly provisioned disposable pair and its retained active state; never enable it on restored or substituted snapshots, or attach a new database to a quarantined wallet. [Backup/restore contract draft](docs/backup-restore.md).

Protect SQLite/WAL files, business ledger, intents, tokens, wallet keys, viewing material and artifact directories. Unix adapter evidence uses restrictive modes; **Windows ACL hardening is not implemented** and files inherit host permissions. There is no application-level encryption or authenticated audit chain. Digests detect inconsistent bytes, not an attacker rewriting all related state. Process-crash tests do not establish power-loss durability, production safety, distributed ownership, public-network or NU7 support.

## Reproduce checks

```sh
cargo +1.95.0 test --locked
python scripts/replay_baseline.py
python integration/test_infrastructure.py
python integration/test_reference_app.py
python tests/http_smoke.py --binary target/debug/zcash-durable-outbox
```

Corrected local validation passed: **43 Rust tests (42 library + 1 CLI)**, formatting, strict Clippy with warnings denied, build, and **10 compiled HTTP/CLI smoke checks with zero remote calls**. [Correction results and hashes](evidence/correction/summary.json). The corrected executable subsequently passed all nine scenarios plus genuine identical-intent A/B CREATE recovery on a fresh disposable stack with the actual verifier. [Corrected real-stack report](docs/corrected-real-stack-acceptance.md). The A/B recovery regression also passes through the production Zallet adapter with Engine/Store: synthetic bytes and a static fixture verifier test provenance, with three local read-only tip/inspect calls for B and zero CREATE calls. This is not actual Zcash artifact verification. On Windows use the `.exe` binary path. Core fixtures exercise real SQLite/concurrency/COMMIT failures; API smoke launches the compiled Rust process in disabled mode; protocol tests exercise local HTTP forwarding or the reference ledger. They are separate from genuine wallet/node acceptance evidence. The preserved [baseline review](docs/baseline-review.md) records 15 model groups and 35 crash boundaries, including the replay's documented process-synchronization adjustment.

The one-shot real-stack runner is `integration/test_real_stack.py --config <private-suite-config.json>`; its default validates inputs and prints a plan. `--execute` performs genuine funded wallet/node scenarios with forwarding proxies and terminal quarantine of two distinct disposable wallets. Read the runner and runbook, provision fresh state and private credentials, and inspect its plan before execution. It does not automatically resume an interrupted run. Historical pre-correction reports are in [integration/evidence/real-stack-final](integration/evidence/real-stack-final/); [acceptance status](docs/acceptance-status.md) distinguishes them from corrected-build checks and remaining work. A passing helper or partial scenario list is not a claim that the whole suite passed.

## Operational release milestones

These safeguards are required release work, not implemented protections:

- Establish externally anchored freshness and a verified restore procedure covering the **business ledger, Outbox and wallet together**; block sending on missing, stale or inconsistent state.
- Enforce and verify disabled wallet broadcasting plus exclusive construction authority, including a second client or Outbox database targeting the same wallet. Current live mode relies on provisioned assumptions.
- Qualify NU7 with exact wallet/node/verifier pins and actual transactions; complete genuine expiry/rejection experiments with no replacement payment or balance restoration.
- Qualify broader prove/sign/extract recovery and safe-error classification without ownership clearing. Keep the one-positive-input restriction until any broader profile has its own evidence.
- Obtain independent external review of operation binding, txid migration, restore freshness and wallet authority; extend the separately recorded corrected qualification for any broader profile. Historical results stay distinct.

[Acceptance criteria](docs/acceptance-status.md), [restore plan](docs/backup-restore.md), and [recovery contract](docs/recovery.md) specify observable gates. `--live-config` does not satisfy these gates; the current live mode has no enforced restore verification.

## License

MIT for the new implementation and documentation. The supplied feasibility baseline is preserved under `reference/`; upstream Zallet, Zebra and librustzcash source is not redistributed here. The proposal's funded work, independent review, adoption and support commitments remain proposed future work.
