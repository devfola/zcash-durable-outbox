# Corrected-build real-stack acceptance

**PASS: all nine scenarios and genuine identical-intent A/B CREATE recovery**, on 20 October 2026, using the corrected executable, real Zallet/Zebra and the actual independent PCZT verifier. The A/B experiment used the same primary Outbox database and wallet as the nine scenarios. No mock wallet/node, synthetic PCZT or static fixture verifier was used. [Run summary](../integration/evidence/real-stack-corrected-001/summary.json), [tested artifacts](../integration/evidence/real-stack-corrected-001/tested-artifacts.json), [A/B evidence](../integration/evidence/real-stack-corrected-001/bound-create-recovery/summary.json), [aggregate verification metadata](../evidence/corrected-real-stack-verification.json).

This qualifies the bounded **disposable regtest NU6.3/V6/Ironwood** profile: exactly one positive sender input, one recipient, two through eight actions and independently verified internal change. It is not an operational or public-network release. Unsupported shapes and ordinary errors after invocation may quarantine the dedicated wallet; no error authorizes clearing its owner or replacing its database.

## Exact tested artifacts

The run retained these SHA-256 values and complete per-file source manifests. Documentation changes after the run do not change its frozen core/harness scope.

| Artifact | SHA-256 |
|---|---|
| Corrected service executable | `1bfe1868435b39329e344e65187acc7ee573e87f856136d859cb346394c06794` |
| Independent verifier executable | `70113de6ee5fd50ee22f8f310e5fcaacb79e5f643c11635073dbf767062efc9f` |
| Core source manifest | `5e1813a77a6754c7588b4b50882d00616201de5114450a5b7adb2ca7a2d9044e` |
| Harness source manifest | `61615493cb8b62854e345a1f523660c5d19f29f97f0c182ad0d7bb6f34b6983b` |
| Nine-scenario orchestrator | `535c72c414da5233fc41e852a0ff8bf6280490dda21d1fc1c4889938526cbe8d` |
| A/B recovery orchestrator | `34b18947c62ca86ea53ff8b918a61c4014c4b0ea04757c47f568444be9713632` |
| Core Cargo.lock | `8ed8d5c62e179400ff5f7d71c504094bce0cb95d79db620548f8d261058a59e6` |
| Verifier Cargo.lock | `22490b30ebc490f24340a8029ad454cba90dd48c0315b849c2ca8d78925d92e0` |

Runtime pins were official Zallet `v0.1.0-beta.3`, source `987382f67e622915228686e9f956c6a9c9a7514c`, Zaino backend; Zebra `v6.2.3`, source `7121c82ba6795a151523d5b308a19743f4a1ade7`; and librustzcash `1f6bb2072e7fcb142b0d90ff7b267a8699a84818`. NU6.3 branch `37a5165b` and the exact regtest genesis/activation map were checked.

The fresh sibling `/home/outbox/corrected-run-001` reused checksum-verified official binaries in the existing Linux VM. It began with a new chain and wallets, completed synthetic funding at height 107, and restarted the funded sender with broadcasting disabled. The actual registration indices were sender `0` and receiver `4609838961`. Both old environments were preserved: before/after SHA-256 manifests matched across 622 files. Private viewing material, credentials and artifacts are omitted from public reports. [Fresh environment](../integration/evidence/corrected-fresh-environment.json), [activation snapshot](../integration/evidence/corrected-fresh-environment-node-activated.json), [executed stopped-state provisioning wrapper](../integration/evidence/real-stack-corrected-001/provisioning/provision_corrected_fresh.py).

## Nine scenarios

| # | Corrected-build result | Evidence |
|---|---|---|
| 1 | PASS: ordinary payout and completed repeat; one CREATE/prove/sign/extract pipeline, independently checked receiver amount/memo and mining. | [record](../integration/evidence/real-stack-corrected-001/01-ordinary-and-repeat.json) |
| 2 | PASS: lost node acceptance response followed by application restart; resubmitted identical committed bytes, with no added construction. | [record](../integration/evidence/real-stack-corrected-001/02-lost-node-response-restart.json) |
| 3 | PASS: four concurrent same-ID requests produced one real pipeline; one HTTP 200 and three 409 conflicts. | [record](../integration/evidence/real-stack-corrected-001/03-concurrent-same-id.json) |
| 4 | PASS: five changed-intent conflicts, with no additional construction. | [record](../integration/evidence/real-stack-corrected-001/04-changed-intent-rejected.json) |
| 5 | PASS: two IDs serialized wallet stages; bounty/refund ledgers retained their IDs/txids and confirmed paid observations through actual service/application restarts. | [record](../integration/evidence/real-stack-corrected-001/05-two-ids-and-reference-app.json) |
| 6 | PASS: actual deferred-foreign-key final COMMIT rejection after the real wallet pipeline; no committed bytes or submission attempt, extraction owner/fence retained on the separate wallet. | [record](../integration/evidence/real-stack-corrected-001/06-real-sqlite-commit-rejection.json) |
| 7 | PASS: application caller killed before extraction completed; remote completion occurred, response was withheld, owner/fence remained, repeat/another-ID requests conflicted and no submission occurred. | [record](../integration/evidence/real-stack-corrected-001/07-wallet-response-loss-quarantine.json) |
| 8 | PASS: actual application restart and remote completion after application death, with durable ownership retained. **No wallet-process restart was performed or imported into this run.** | [record](../integration/evidence/real-stack-corrected-001/08-app-restart-and-remote-completion.json) |
| 9 | PASS: stored-byte mismatch in a copied database rejected with no construction/submission; original database unchanged. | [record](../integration/evidence/real-stack-corrected-001/09-integrity-copy-rejected.json) |

Both terminal wallets remain quarantined. Claims were not cleared, stolen or bypassed through another database. A final read-only inspection recorded primary ownership at `terminal-extraction-response-loss`, secondary ownership at `terminal-sqlite-commit-rejection`, and node height 113. The guest was then shut down gracefully; host process inventory confirmed both the task VM and SSH tunnel had exited, preserving all state. [Post-run inspection](../integration/evidence/real-stack-corrected-001/post-run-inspection.json), [lifecycle record](../integration/evidence/real-stack-corrected-001/lifecycle.json). Process restart, a corrupted-copy check and transaction verification do not establish verified restore freshness.

## Genuine identical-intent A/B recovery

A and B shared approved-intent digest `0163f3f6a918b71da728f2f73ce3bcce5897702b641bedadb74173bcfddcd5a0`, but had distinct payment IDs, owners and CREATE operation IDs. Before each upstream CREATE dispatch, a read-only database snapshot and original `binding.json` were observed to carry the matching operation/payment/owner/digest tuple.

The B-specific diagnostic fault aborted the Outbox artifact INSERT after B's genuine CREATE response was saved. A's saved response was then rejected for B **before any RPC**, with B's state unchanged. After the diagnostic trigger was removed while the service was stopped, B's original bound response recovered under B's original owner. Recovery made one `getblockchaininfo`, one `getblockhash` and one `pczt_inspect` call, and **zero CREATE replays**. No owner/payment row was manually edited and no quarantine bypass occurred.

Both genuine created PCZTs passed independent effects verification. Normal continuation produced two calls to each of CREATE, prove, sign and extract, and two node submissions, with normal final COMMIT releasing the construction slot. The actual verifier checked final payment effects, all 512 memo bytes, one positive input, change, proofs, authorization, raw response and independently derived consensus txid. The receiver observed both payments mined with one confirmation.

| Payment | Independently computed txid | Verified shape |
|---|---|---|
| A | `e29c695f9b7f8e9d9f32ec7c819046a0625e38ee0d4e977f0391b94958d60202` | 100000 zatoshi, five actions, one positive input, 25000-zatoshi fee |
| B | `33b9affc7b4bc9d15f03ff6214c66494b4e90f69ff32c8024b50ae132f1202dd` | 100000 zatoshi, two actions, one positive input, 10000-zatoshi fee |

The [A/B summary](../integration/evidence/real-stack-corrected-001/bound-create-recovery/summary.json) retains source response/PCZT hashes, pre-dispatch binding observations and final verification reports. Legacy evidence lacking a binding durable before CREATE remains ineligible; this experiment does not make historical unbound artifacts recoverable.

The [in-session evidence review](../integration/evidence/real-stack-corrected-001/bound-create-recovery/independent-review.json) matched the saved bindings, PCZTs, helper reports and final artifact chains. Receiver observations and cross-adoption row equality were asserted by the pinned harness; full original receiver replies and database snapshots were not separately retained.

## Separate evidence and remaining gates

The earlier [nine-scenario suite](../integration/evidence/real-stack-final/summary.json), [controlled reorg](../integration/evidence/controlled-reorg/summary.json), [unbound saved-CREATE supplement](../integration/evidence/saved-create-recovery/result.json) and wallet-process restart experiments remain historical evidence for their exact recorded builds. They were not relabeled or reused as new passes. This corrected run did not repeat the reorg or wallet-process restart. The separate [local correction report](../evidence/correction/summary.json) remains unchanged: 43 Rust tests and 10 compiled HTTP/CLI smoke checks; its synthetic adapter fixture is distinct from the genuine A/B experiment above.

Operational release still requires verified freshness/restore across business ledger, Outbox and wallet; enforced/verified disabled broadcasting and exclusive construction authority; NU7; actual expiry/rejection; broader safe recovery/error classification; and external review. The tested wallets' disabled broadcasting and exclusive access are provisioning assumptions, not adapter-enforced attestations. [Acceptance milestones](acceptance-status.md), [restore plan](backup-restore.md), [recovery contract](recovery.md).
