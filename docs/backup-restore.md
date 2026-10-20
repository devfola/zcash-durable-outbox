# Backup and restore contract draft

**Verified backup/restore is an unimplemented operational-release gate.** This document is a qualification plan, not a tested restore facility. The default executable mode disables wallet construction and publication. Explicit `--live-config` enables the bounded disposable-regtest adapter and can publish existing valid committed records; it does **not** verify whether those records were restored.

No restore detection, external freshness anchor, restore manifest, verification record or restore-specific send-enabling gate exists. A stale but internally valid database can pass its checks while omitting later payments. Wallet/Outbox snapshot skew cannot be ruled out by a checksum, SQLite integrity check or matching account UUID. Do not use live configuration on restored or substituted state. The supported prototype path starts with a freshly provisioned disposable wallet/outbox pair and retains that active pair through controlled process restarts.

The historical nine-scenario and unbound-recovery experiments used the pre-correction binary. A new corrected nine-scenario/A-B run now qualifies the bounded CREATE-origin path, but neither run qualifies a restore procedure. Corrected local validation passed: **43 Rust tests (42 library + 1 CLI)**, formatting, strict Clippy with warnings denied, build, and **10 compiled HTTP/CLI smoke checks with zero remote calls**. [Correction results and hashes](../evidence/correction/summary.json). The corrected executable subsequently passed all nine scenarios plus genuine identical-intent A/B CREATE recovery on a fresh disposable stack with the actual verifier. [Corrected real-stack report](corrected-real-stack-acceptance.md). Exactly one positive Ironwood input is supported; unsupported shapes and ordinary errors after invocation may quarantine the dedicated wallet. Restoring a snapshot or clearing ownership is not a recovery for that condition.

## Preserve the whole state boundary

Zallet's wallet database, Outbox SQLite database and reference application's business ledger are separate durable objects. An extraction committed by the wallet may be missing from the Outbox after a lost response, failed COMMIT or restored snapshot. A later committed payment can be absent from an older valid business ledger. Saved signed PCZT, owner and in-flight marker are evidence; they do not establish remote rollback or justify another ID or construction pipeline.

Preserve the Outbox database and required WAL content, business ledger/WAL, private PCZT/raw artifacts, wallet state and key material, trusted viewing registration, durable operation/payment/owner/intent-digest bindings, configuration, credentials, stage/proxy journals, and exact software/network/upgrade pins. Keep spending and viewing material out of public evidence. The live setup assumes `external.broadcast=false` and exclusive wallet access; the adapter does not independently attest those conditions.

Operator backups need reviewed encryption and access controls. Application-level encryption, an authenticated audit chain and Windows ACL hardening are not implemented. Unix adapter evidence uses restrictive modes, but that is not a complete backup protection policy. Digests detect mismatches within retained state, not an attacker or restore operation replacing all related state consistently.

## Candidate backup procedure to qualify

1. Stop new application work. Quiesce the construction authority and determine whether a remote RPC remains active. Caller death or timeout alone does not establish quiescence. Preserve uncertainty and keep sending disabled when it cannot be established.
2. Preserve operation evidence, then stop wallet, Outbox and business application cleanly. Never clear ownership or edit wallet data to make a snapshot look consistent. Use a qualified wallet backup/export mechanism and SQLite-aware backups of both application databases. Copying only an active `.db` file while ignoring its WAL is insufficient.
3. Record a protected manifest with software/schema versions, database/operator identity, network and upgrades, wallet/account/viewing registration, backup lineage, chain observations, payment IDs, canonical-intent digests, committed txids/byte digests, unresolved owners/stages, CREATE-origin bindings and content hashes. A legacy artifact with no pre-invocation binding remains quarantined; the backup process must not manufacture one. A freshness record outside the backed-up failure domain is needed to detect a consistent older restore; its design remains open.
4. Verify readability and hashes in an isolated restore drill while publication stays blocked. Preserve the original snapshot. A readable backup is not authority to send.

These steps have not been qualified as a complete backup procedure against genuine wallet/Outbox/business-ledger snapshots. Ordinary process restart and artifact verification do not substitute for this drill.

## Candidate restore verification to qualify

Restore into an isolated directory and inspect with publication disabled. Verify manifest freshness, schema/integrity, operator and database lineage, exact wallet/account/network/upgrades, canonical intents and their digests, completed artifacts, immutable final bytes and actual consensus transaction IDs. For any proposed saved-CREATE adoption, independently require the exact operation/payment/owner/intent-digest binding that was durable before its RPC; matching effects alone is insufficient. Reconcile every unresolved owner and operation against the actual wallet and chain. Account for extraction completing after the application's snapshot without constructing another payout.

For committed payments, retain and inspect the existing bytes. Verify wallet spent-input accounting and independent node/receiver observations. A matching txid or acceptance receipt does not authorize substituting re-extracted bytes: actual repeated extraction changed authorization bytes under the same consensus ID. Reconcile the business ledger using its original IDs and thresholds. Never restore an application balance automatically, invent an ID, release a claim after elapsed time, or rerun construction to make snapshots agree.

If any state is absent, stale, inconsistent or uncertain, retain the evidence and keep publication disabled. No command can mark a restore verified. `--live-config` is **not** that missing verification command: using it would bypass the planned operational gate rather than satisfy it. A reviewed, enforced transition remains to be implemented and tested before operational use.

## Required qualification tests

Test active-WAL backup, process crash during snapshot, an older internally valid restore, business-ledger rollback, wallet/Outbox skew after extraction and after final-byte commitment, missing/corrupt artifacts, wrong wallet/account/network, and an RPC finishing after its caller disappears. Establish that an enforced restore gate prevents every unverified restore from creating a submission attempt or reconstructing a committed payment. Then verify any allowed recovery against actual node and receiver state.

Those complete restore drills and the enforced gate are not claimed here. Default-mode blocking tests establish only that an operator can run the disabled configuration; they do not establish automatic restore recognition. This remains a release limitation alongside NU7 compatibility and qualified general precommit reconciliation.

## Explicit restore-release acceptance

This milestone spans **business ledger + Outbox + wallet**, including their WALs, artifacts, operation bindings, unresolved ownership, chain observations and externally retained freshness/lineage state. No implemented safeguard is claimed by this plan.

- A tested snapshot procedure must establish quiescence or record unresolved remote work; the latter cannot silently become a clean restore.
- A deliberately older, internally valid copy of any one store must be detected against an independent freshness anchor. SQLite integrity, digests and matching wallet/account are insufficient.
- Active-WAL snapshots, business-ledger rollback, wallet/Outbox extraction skew, final-COMMIT skew and lost remote responses must each leave sending blocked until a reviewed reconciliation establishes consistency.
- Tests must show zero construction and zero submission attempts for every missing, stale, mismatched or unverified restore. The live-configuration option must not bypass the eventual enforced decision.
- A duplicate non-null txid discovered during migration must cause a safe transactional refusal. Preserve conflicting rows, bytes and claims; do not merge/delete/reassign IDs as a restore shortcut. This additional invariant does not replace operation provenance or freshness.

Before an operational release, also enforce and verify disabled wallet broadcasting and exclusive construction authority; qualify NU7, actual expiry/rejection and broader recovery/error classes; and obtain external review. Those separate milestones are specified in [acceptance status](acceptance-status.md). None is satisfied by the existing manual publication setting, historical test pass, or an operator assertion that a restore looks current.
