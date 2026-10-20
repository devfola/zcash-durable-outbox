# Acceptance status after the saved-CREATE correction request

**PASS: the corrected executable passed all nine scenarios plus genuine identical-intent A/B CREATE recovery on a fresh disposable Zallet/Zebra stack with the actual independent verifier.** [Corrected report and exact hashes](corrected-real-stack-acceptance.md). The missing CREATE-origin check was confirmed: matching request/intent/artifact details and an existing owner did not establish the originating operation. Two IDs with identical details could cross-adopt responses from different operations. No on-chain loss was demonstrated.

The earlier nine real-stack passes and supplementary reorg/unbound-recovery results below are **historical qualification of the pre-correction binary**, identified by their retained hashes. They remain useful records of those experiments, but do not qualify the corrected build or establish cross-payment operation binding. The new corrected run has its own evidence and hashes; historical results were not relabeled.

**Exactly one positive Ironwood sender input is supported.** Multiple positive inputs and other unsupported shapes are rejected. An unsupported shape or ordinary-looking error after a wallet operation begins may quarantine the entire dedicated wallet. Failure paths retain ownership/in-flight state; they do not clear a claim, retry with a new ID or attach a new Outbox database to that wallet.

## Corrected-build regression acceptance

The corrected build passes the local regression suite for the checks below. Its local fixtures are separate from the subsequent genuine A/B experiment, which also passed with the actual PCZT verifier. General safe-error classification remains an operational milestone.

| Correction | Required observable result |
|---|---|
| Durable CREATE origin | Before Zallet CREATE is called, persist a unique operation ID bound to payment ID, existing owner and canonical-intent digest; save the matching binding with the original evidence. Recovery compares that exact active tuple. |
| Two IDs with identical details | A and B have the same recipient/amount/memo but distinct operations. A's response is rejected for B with no wallet call or state change; B's own response recovers under B's original owner with zero CREATE replay. Both the Engine fixture and production Zallet recovery adapter with Engine/Store pass; the adapter fixture uses synthetic bytes, a static verifier and three local read-only tip/inspect calls for B. That local fixture does not verify an actual Zcash artifact. The separate fresh real-stack A/B test also passes with genuine PCZTs and the actual verifier. |
| Missing/mismatched provenance | Wrong operation/payment/owner/digest and legacy evidence without binding are rejected; claims/fences remain. Do not infer or backfill a binding from content equality or filenames. |
| Unique non-null txid | Duplicate non-null committed txids, including case variants, cannot be associated with separate records; pending null txids remain allowed. This adds an invariant and does not replace origin binding. |
| Safe migration | A legacy database with duplicate non-null txids causes migration to refuse atomically, preserving schema/data/bytes/owners with no automatic deletion, merge, reassignment or RPC. |
| Safe errors versus uncertainty | Errors proven to occur before an operation starts are distinguished from errors after durable invocation/dispatch. Unsupported shape, failed verification, RPC error or timeout after start must not release ownership automatically. |

See the [required recovery contract](recovery.md). Corrected local validation passed: **43 Rust tests (42 library + 1 CLI)**, formatting, strict Clippy with warnings denied, build, and **10 compiled HTTP/CLI smoke checks with zero remote calls**. [Correction results and hashes](../evidence/correction/summary.json). The corrected executable subsequently passed all nine scenarios plus genuine identical-intent A/B CREATE recovery on a fresh disposable stack with the actual verifier. [Corrected real-stack report](corrected-real-stack-acceptance.md).

## Corrected real-stack acceptance

All nine scenarios and the genuine A/B binding/recovery experiment passed under the separately recorded corrected executable, verifier and core/harness manifests. Positive payments have independent final verification and mined receiver observations. Scenario 8 covers actual application restart and remote completion after application death; no wallet-process restart was performed or imported into this run. Both terminal wallets remain quarantined. [Nine-scenario table, A/B details and hashes](corrected-real-stack-acceptance.md), [new summary](../integration/evidence/real-stack-corrected-001/summary.json).

## Historical real-stack acceptance

The [historical summary](../integration/evidence/real-stack-final/summary.json) binds its service, verifier, both Cargo.lock files and orchestrator by SHA-256. Those hashes did not change during that run. Positive payments were checked against exact node bytes and receiver-wallet mined observations. Zallet/Zebra ran in the workspace-local Linux VM; the service/SQLite ran on Windows.

| Scenario | Historical result for that pre-correction binary | Evidence |
|---|---|---|
| 1. Ordinary payout and completed repeat | PASS: one pipeline; receiver amount/memo/mining checked; no added construction on repeat. | [record](../integration/evidence/real-stack-final/01-ordinary-and-repeat.json) |
| 2. Lost node acceptance response | PASS: app restart resubmitted identical committed bytes with no reconstruction. | [record](../integration/evidence/real-stack-final/02-lost-node-response-restart.json) |
| 3. Same-ID concurrency | PASS: one pipeline and retained-state conflicts. | [record](../integration/evidence/real-stack-final/03-concurrent-same-id.json) |
| 4. Changed intent | PASS: recipient, amount, memo, fee and expiry changes rejected. | [record](../integration/evidence/real-stack-final/04-changed-intent-rejected.json) |
| 5. Two IDs/reference app | PASS: serialized stages and durable bounty/refund identity across restarts. This did not test cross-adopting identical-intent CREATE operations. | [record](../integration/evidence/real-stack-final/05-two-ids-and-reference-app.json) |
| 6. Actual final COMMIT rejection | PASS: genuine deferred-FK rejection, no final bytes/attempt or node submission; extraction fence retained. | [record](../integration/evidence/real-stack-final/06-real-sqlite-commit-rejection.json) |
| 7. Lost extraction response | PASS: wallet completion after caller death, retained original owner/fence, no replay or submission. | [record](../integration/evidence/real-stack-final/07-wallet-response-loss-quarantine.json) |
| 8. Restart/remote completion | PASS for the recorded app restart and external controlled wallet-restart evidence. | [record](../integration/evidence/real-stack-final/08-app-restart-and-remote-completion.json) |
| 9. Stored-byte mismatch | PASS: corrupted copy rejected without reconstruction/submission; original DB preserved. | [record](../integration/evidence/real-stack-final/09-integrity-copy-rejected.json) |

The terminal wallets remain quarantined. Their claims were never cleared, timed out, stolen or bypassed with another database. Earlier port/index debugging evidence and aborted runs remain preserved, rather than being rewritten as corrected results.

The [historical saved-CREATE supplement](../integration/evidence/saved-create-recovery/result.json) continued under the same owner without CREATE replay and then reached a real COMMIT rejection. It lacked the new origin-binding regression and must not be described as qualification of corrected recovery. The [controlled reorg](../integration/evidence/controlled-reorg/summary.json) recorded paid true → false → true, restored the original chain, and made no construction/submission calls; it also belongs to its recorded pre-correction build.

Other historical evidence includes [18 real-artifact verifier checks](../integration/evidence/real-pczt-verifier/summary.json), [changed-byte extraction](../integration/evidence/wallet-boundary/summary.json), [wallet restart during dispatched proving](../integration/evidence/wallet-restart-during-prove.json), and [executed fresh provisioning](../integration/evidence/fresh-runtime-funding.json). The prior 32 Rust tests and local API/protocol/model checks are not corrected-build regression results.

## Operational release milestones

The following operational protections remain **unimplemented or unqualified**. Passing corrected local fixtures does not satisfy these release gates:

| Milestone | Concrete release acceptance criteria |
|---|---|
| Verified freshness/restore across all three stores | Protect and reconcile business ledger, Outbox and wallet as one recovery boundary, with an independently retained freshness/lineage anchor. Exercise active WAL, old-but-valid snapshots, business-ledger rollback, wallet/Outbox skew and remote completion after snapshots. Missing/stale/inconsistent/unverified restores must make zero wallet construction and zero submission attempts. `--live-config` must not bypass the enforced verification decision. |
| Enforce and verify the sole-publisher boundary | Verify disabled wallet broadcasting and enforce exclusive construction authority, including another RPC client or Outbox database targeting the same wallet. Demonstrate rejection when broadcasting is enabled, unknown or changes, and when a second construction authority attempts entry. Provisioned settings alone are insufficient. |
| NU7 | Publish exact node/wallet/verifier revisions and upgrade parameters, then demonstrate actual supported shielded transactions, independent verification and relevant failure tests at NU7. The NU6.3 binaries/fixtures do not satisfy this gate. |
| Actual expiry and node rejection | Observe genuine expiry and genuine Zebra rejection/uncertainty against retained committed bytes. Preserve IDs/owners/artifacts/accounting, distinguish observations from error strings, and show no replacement construction or automatic business-balance credit. |
| Broader recovery and error classification | Define evidence/quiescence requirements for each permitted prove/sign/extract recovery and each proven-safe error. Test failure windows around RPC, wallet storage and Outbox COMMIT. Unknown operations remain quarantined; neither timeout nor ordinary error clears ownership. Never re-extract after byte commitment. |
| External review and broader qualification | Corrected local and fresh real-stack reports now exist under separate hashes. Obtain external review of operation binding, migration, wallet exclusivity and restore freshness; qualify broader recovery and any broader input support separately. The current profile remains exactly one positive input. |

Default mode blocks construction/publication, but live mode currently has no enforced freshness/restore gate and relies on provisioned `external.broadcast=false` and exclusive access. Windows ACL hardening, application encryption and power-loss qualification are not implemented. [Restore plan](backup-restore.md), [compatibility](qualification-compatibility.md), [recovery contract](recovery.md).
