# Saved-CREATE recovery: required correction contract

**The pre-correction recovery check was insufficient.** Request/intent/PCZT equality and an existing owner did not establish which payment's operation produced a saved response. Two payment IDs with identical recipient, amount and memo could match those checks while referring to different CREATE operations. The missing durable origin binding was confirmed. No on-chain loss was demonstrated.

The recorded saved-CREATE success and nine-scenario run are historical results for their pre-correction binary. They do not qualify this correction. Corrected local validation passed: **43 Rust tests (42 library + 1 CLI)**, formatting, strict Clippy with warnings denied, build, and **10 compiled HTTP/CLI smoke checks with zero remote calls**. [Correction results and hashes](../evidence/correction/summary.json). The corrected executable subsequently passed all nine scenarios plus genuine identical-intent A/B CREATE recovery on a fresh disposable stack with the actual verifier. [Corrected real-stack report](corrected-real-stack-acceptance.md).

## Required durable origin binding

Before the first Zallet CREATE call, one database transaction must persist a new operation ID bound to all of:

- The immutable payment ID.
- Its existing construction owner token.
- Its canonical-intent digest.
- The CREATE operation/stage and its in-flight state.

The corrected evidence layout is `create-{operation_id}/binding.json`, containing `OperationBinding {operation_id, payment_id, owner, intent_digest}`. Save it with the original request/evidence before forwarding the RPC. The operation must retain that origin through its response and artifact records. A `create-<UUID>` directory name is not sufficient unless that operation ID was durably bound to this payment before the call. Matching recipient, amount, memo, account, full intent or independently verified payment effects does not identify the originating operation.

The correction uses schema 3's immutable `create_operations` table for `operation_id`, unique `payment_id`, `owner` and `intent_digest`; the evidence view exposes `create_operation` metadata. Corrected local fixture/build checks and the subsequent genuine A/B real-stack experiment passed; see their separate reports above. Legacy records are not backfilled.

Recovery must match the durable operation ID, payment ID, owner and intent digest **exactly** against the active database operation and the saved evidence. It must still require the same wallet/account, correct stage, retained wallet slot, unchanged artifacts and all independent checks. The adoption transaction must compare the expected operation as well as the existing owner/stage. It must not substitute a later current operation after validation.

**Legacy evidence without the durable binding remains quarantined.** Do not manufacture or backfill an origin from a filename, content equality, current database row, PCZT metadata or a guessed owner. Additional provenance and unique-txid constraints do not replace the origin binding.

## Required saved evidence and rejection behavior

Retain the original private evidence root and successful CREATE response, exact request parameters, canonical intent, decoded PCZT, and the original `binding.json` carrying the durable operation/payment/owner/intent-digest tuple. Files and artifact bytes must agree; regular-file/path checks and detection of source changes remain necessary. They do not authenticate the complete history against an attacker able to replace every related file.

A response from operation A must be rejected for payment B even if A and B have byte-identical intent details. Wrong, absent, legacy or changed binding must leave payment, owner, in-flight marker and wallet slot unchanged. Reject before invoking wallet RPCs for an invalid binding. Never attempt CREATE again to obtain a matching response.

The required regression uses two IDs with identical details and distinct operations: reject A's response for B; retain B's original operation/owner; then permit B's own bound response under B's same owner with **zero CREATE replay**. It passes through both the Engine fixture and the production Zallet recovery adapter with Engine/Store. The latter uses synthetic artifact bytes and a static fixture verifier: A's evidence reaches no RPC, while B's matching evidence makes three local read-only tip/inspect calls and no CREATE call. It tests provenance, not actual Zcash artifact verification. The local negative checks cover changed owner, operation ID and digest, legacy unbound evidence, and owner/slot/stage comparisons. The tuple is immutable and atomically rechecked; no dedicated check-to-commit race-hook test is claimed. These corrected local regressions pass; the correction report above records their scope and hashes.

The subsequent genuine A/B experiment used the same primary database/wallet as the corrected nine-scenario suite. A and B had the same approved intent digest but distinct pre-dispatch operation/payment/owner bindings. A was rejected for B before any RPC with state unchanged; B's original response recovered under B's original owner with zero CREATE replay. Actual created PCZTs and final transactions passed the independent verifier, and both receiver payments were mined. The diagnostic B-specific artifact-INSERT trigger was removed only while the service was stopped; owner/payment rows were not edited. [Genuine recovery evidence](../integration/evidence/real-stack-corrected-001/bound-create-recovery/summary.json), [report and hashes](corrected-real-stack-acceptance.md).

## Narrow operator route

The CLI name remains `reconcile-create ID --response PATH`; there is no HTTP recovery endpoint. Its supported purpose is adoption of an already received successful CREATE response after exact origin-binding/provenance and independent payment-effect verification. Use only corrected code whose binding contract and regressions have been verified. The historical artifacts cannot be upgraded into eligibility by editing their provenance.

Keep the original Outbox database, dedicated wallet/account, trusted viewing material and evidence root. Stop the Outbox service while inspecting the proposed recovery. Inspection is not authority to resume. Correcting an address diversifier index must be justified from actual saved account/address registration using the unchanged account and UFVK; that correction alone does not establish the CREATE operation's origin.

A permitted adoption atomically stores the completed created artifact, advances the same payment, and clears only that exactly matched completed CREATE marker under its original owner. It neither replaces/releases the owner nor reports payment acceptance. Subsequent retry may continue only from a durably completed artifact with no uncertain operation, under the same owner and normal stage/operation comparisons. The binding does not authorize arbitrary artifact import or general recovery.

## Profile limits and quarantine

**Exactly one positive Ironwood sender input is supported**, with one recipient and the declared action/change bounds. Aggregate wallet funds are not enough to establish that a payment fits this profile: selecting multiple positive inputs is unsupported. Verification can reject such a PCZT after CREATE has already run.

A provably local error before an operation begins differs from an error after durable invocation or RPC dispatch. Once an operation may have run, a familiar validation error, unsupported shape, timeout, disconnected caller or failed COMMIT is not proof of remote cancellation/rollback. Such errors can quarantine the entire dedicated wallet. The failure path retains ownership and its in-flight fence. No generic error handler, elapsed timeout or new payment ID may clear them.

Missing responses, uncertain proving/signing/extraction and rejected final-byte COMMIT remain outside this CREATE route. Wallet extraction can already be stored, and repeated extraction can change authorization bytes under the same txid. Do not clear owners, edit wallet databases, attach a fresh Outbox database to the quarantined wallet, substitute snapshots or reconstruct after final-byte commitment. Eligible postcommit retry uses only the committed bytes.

## Additional txid and release safeguards

The locally tested database invariant is case-insensitive uniqueness of every non-null committed txid within the Outbox database, with multiple pending null values permitted. Migration must first detect legacy duplicate non-null txids and refuse safely: report the conflict, preserve rows/bytes/claims and schema state, and make no wallet/node calls. Do not delete, merge, reassign or choose a winning business ID automatically. Corrected migration/constraint tests remain separate from CREATE-origin tests; txid uniqueness is not a provenance check.

Operational release also requires verified freshness/restore across business ledger, Outbox and wallet; enforced/verified disabled wallet broadcasting and exclusive construction authority; NU7; actual expiry/rejection; broader safe recovery classification; and external review. These are milestones, not safeguards claimed implemented here. [Acceptance criteria](acceptance-status.md), [restore plan](backup-restore.md).
