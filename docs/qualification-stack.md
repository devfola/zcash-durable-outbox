# Actual local stack and disposable funding

The recorded Zallet/Zebra experiments ran inside a workspace-local Linux VM on
the Windows host. Both release binaries were executed, both disposable wallets
initialized, regtest mined, and the sender funded with an actual Ironwood note.
The sender was then stopped, its publication setting changed to false, restarted,
and its shielded balance read again. No Windows installer, driver, feature change,
WSL installation or administrator operation was needed.

This establishes the environment and funding boundary. PCZT payment, response-loss,
receiver and service crash evidence is reported separately; this report does not
turn source inspection or infrastructure tests into those results. See
[qualification-wallet.md](qualification-wallet.md),
[qualification-compatibility.md](qualification-compatibility.md), and the repository's
acceptance report. The supplied model replay and proposal are described in
[baseline-review.md](baseline-review.md) and [proposal-alignment.md](proposal-alignment.md).

## Qualification scope after the CREATE-origin correction

The earlier nine service scenarios and unbound saved-CREATE supplement referenced here qualify their **historical pre-correction binary**, not the corrected executable. The missing operation-to-payment binding was confirmed; no on-chain loss was demonstrated. The correction requires a durable operation/payment/owner/intent-digest tuple before CREATE and exact matching original binding evidence. Legacy unbound evidence remains quarantined. Corrected local validation passed: **43 Rust tests (42 library + 1 CLI)**, formatting, strict Clippy with warnings denied, build, and **10 compiled HTTP/CLI smoke checks with zero remote calls**. [Correction results and hashes](../evidence/correction/summary.json). The corrected executable subsequently passed all nine scenarios plus genuine identical-intent A/B CREATE recovery on a fresh disposable stack with the actual verifier. [Corrected real-stack report](corrected-real-stack-acceptance.md).

Exactly one positive Ironwood input is supported. Unsupported shapes or ordinary errors after operation invocation can quarantine the dedicated wallet; failure does not clear ownership. The provisioning/publication settings and exclusive access remain assumptions rather than enforced attestations. [Current acceptance](acceptance-status.md), [corrected recovery contract](recovery.md).

## Exact selected runtime

| Component | Executed pin |
|---|---|
| Zallet release | `v0.1.0-beta.3`, source `987382f67e622915228686e9f956c6a9c9a7514c` |
| Wallet backend | Official `zallet-zaino` binary; embedded chain backend reaches Zebra by JSON-RPC; no external Zaino daemon |
| Zallet archive SHA-256 | `1df2398df016ae6e9a1cba3f8ef0cc86ca0d7b48a0934b34df6f68114ebe9306` |
| Zebra | `v6.2.3`, source `7121c82ba6795a151523d5b308a19743f4a1ade7` |
| Zebra archive SHA-256 | `16305a3071f35d1b09790e6713ff4db30c9fb31a110c569eaa6386404079bd0e` |
| Node features | Official default release features, observed in startup log; neither indexer nor background internal miner required |
| Dependency cohort | Zebra chain `11.3.0`, RPC `15.0.0`, state `12.0.1`; on-disk node format `28.0.0` |
| Wallet dependency patch | librustzcash `1f6bb2072e7fcb142b0d90ff7b267a8699a84818` |
| Guest | Ubuntu Minimal 24.04 image release `20261017`, kernel `6.8.0-147-generic` |
| Hypervisor process | QEMU Windows archive `20260811`, reported version `11.1.0 (v11.1.0-12130-ge470268ff4)` |
| VM resources | Existing WHPX acceleration; `pc,kernel-irqchip=off`, CPU `qemu64`, four vCPUs, 6144 MiB RAM, 48 GiB virtual disk |

The official archive digests were checked against GitHub release asset metadata.
Zallet's small provenance JSON binds the archive digest to the release source
commit. Attestation/GPG signatures were not independently verified in this run;
digest/provenance inspection is the precise verification claim.
[Preserved provenance](../integration/evidence/zallet-beta3.provenance.json),
[Zallet release](https://github.com/zcash/zallet/releases/tag/v0.1.0-beta.3),
[Zebra release](https://github.com/ZcashFoundation/zebra/releases/tag/v6.2.3).

Earlier analysis inspected Zallet HEAD
`e5c16c464d2d641e5a7c386f3682cfa37e1c43f9`, whose backend set differs from the
release. The release's six PCZT implementation/common files were compared and
found byte-identical to that inspected HEAD. This does not make the two repository
commits interchangeable. Runtime claims here use the release commit and `zaino`
backend. [Release backend setup](https://github.com/zcash/zallet/blob/987382f67e622915228686e9f956c6a9c9a7514c/book/src/guide/setup.md),
[release backend selection](https://github.com/zcash/zallet/blob/987382f67e622915228686e9f956c6a9c9a7514c/book/src/guide/installation/README.md).

## Network and publication settings

The actual chain is isolated regtest. Overwinter, Sapling, Blossom, Heartwood and
Canopy activate at height 1. NU5, NU6, NU6.1, NU6.2 and NU6.3 activate at height 2.
The active branch is `37a5165b` (NU6.3 / Ironwood). Wallet and node activation
parameters agree, and the node's actual upgrade report was captured.
[Runtime pin/configuration data](../integration/pinned-sources.json),
[real node handoff observation](../integration/evidence/runtime-node-handoff.json).

Zebra reports this regtest's BIP70 `chain` name as `test`. That string alone does
not distinguish it from public testnet. The harness must additionally check
genesis `029f11d80ef9765602235e1bc9727e3eb6ba20839319f761fee920d63401e327`,
the complete activation-height map, and active/next branch `37a5165b` before any
wallet or publication operation. The packaged funding setup includes those checks
and refuses an already-advanced endpoint when expecting its fresh node.

This is not NU7 qualification. The proposal's NU7 release gate remains separate.
The funded shielded note actually reports `pool: ironwood`; an old Orchard-only
transaction assumption would be wrong for this profile.

All guest RPCs bind to loopback: node `29232`, sender `50232`, receiver `50233`.
SSH user-mode networking exposes only guest SSH to host loopback `27222`; a
separate SSH tunnel forwards those three RPC ports to Windows loopback. Node
cookie authentication is explicitly disabled only on this isolated loopback test
node. Both wallet RPCs use synthetic Basic credentials. Spending keys, the SSH
private key, wallet databases, viewing registration and credentials stay in
protected work/guest storage, not the evidence package.

The config's nonempty `[[rpc.auth]] password` without `pwhash` is supported by the
actual release and was exercised. Upstream hashes it in memory and warns about
this form. [Auth schema/implementation](https://github.com/zcash/zallet/blob/987382f67e622915228686e9f956c6a9c9a7514c/zallet-core/src/components/json_rpc/server/authorization.rs#L174).

Both outbox wallets run with `external.broadcast=false`. The sole temporary
exception was initial sender funding: before any business PCZT existed, the
authorized setup enabled wallet publication for one `z_shieldcoinbase`, waited
for success, mined and observed the shielded note, stopped the wallet, disabled
publication, and restarted. This is not used to recover or publish an outbox
payment. No `getrawtransaction` recovery of unbroadcast wallet bytes is assumed.

## Actual provisioning and funding observations

The original native host preflight found Windows, no installed WSL and no Docker.
A locked offline native wallet check could not resolve its git dependency. Those
historical results are preserved, but no longer justify saying Linux execution
is unavailable: the reversible VM route subsequently worked.
[Original preflight](../integration/evidence/host-preflight.json),
[original bounded attempt](../integration/evidence/native-attempt.txt).

QEMU and 7-Zip installer files were downloaded and hash-verified, then unpacked
as archives; neither installer was executed. The Ubuntu image's published
SHA-256 matched. QEMU's initial `q35`/host-CPU boot paused with
`WHPX: Unexpected VP exit code 4`; QMP confirmed `running=false`. Graceful QMP
quit released that failed process. Switching only VM arguments to
`pc,kernel-irqchip=off` and `qemu64` booted Linux and SSH within about 32 seconds.
The later four-CPU reboot reached cloud-init completion in 13.10 guest seconds.
TCG was available but was not needed or benchmarked.
[QEMU/guest checksums](../integration/vm-downloads.json),
[redacted runtime evidence](../integration/evidence/runtime-environment.json).

Two virtual CPUs were insufficient for the release's default DB pool: four
long-lived sync tasks each hold a handle, while deadpool defaults to twice the
logical CPU count. The actual two-CPU wallet returned DB-free `getwalletinfo`
but a DB-backed `z_listaccounts` remained waiting. No account mutation had been
attempted. After preserving that observation, stopping the task-owned processes,
and rebooting the same guest/disk with four CPUs, the account list and account
creation returned. Use the tested four-CPU configuration.
[Wallet pool configuration](https://github.com/zcash/zallet/blob/987382f67e622915228686e9f956c6a9c9a7514c/zallet-core/src/components/database/connection.rs#L44),
[four sync task handles](https://github.com/zcash/zallet/blob/987382f67e622915228686e9f956c6a9c9a7514c/zallet-core/src/components/sync.rs#L145),
[deadpool default size](https://docs.rs/crate/deadpool/0.12.3/source/src/managed/config.rs).

The original manually assembled environment's funding completed:

- Initialized independent sender and receiver age identities, mnemonics and accounts.
- Derived sender UA at index 0 with transparent/Orchard receivers and a shielded receiver UA.
- Mined 102 blocks to the sender transparent receiver after the two upgrade blocks.
- Called `z_shieldcoinbase` once with limit 1 and `AllowRevealedSenders`; the real operation succeeded in 17 seconds and reported `broadcast=true`.
- Funding txid: `b6687603a705d2d25cf367d2563132c29d2a5f0bd35d897b3b8ddc92e0d1dc11`.
- Mined three further blocks; actual wallet notes included an Ironwood note. Sender private balance was `6.24985000` synthetic ZEC.
- Stopped the sender, changed publication to false, restarted it, and read the same positive private balance. The host tunnel then read actual node height 107.

Two setup corrections were driven by real responses and preserved: node cookie
auth defaults to true, and release `z_gettotalbalance` requires
`include_watchonly=true`, returning the balance under `private`. Failed setup
reads were not wallet mutation retries. Account/mining/shielding artifacts were
checked before narrow continuations; no mutating funding call was repeated after
an uncertain response. [Redacted funding observation](../integration/evidence/runtime-funding.json).

## Executed packaged fresh-root rehearsal

After preserving the original experiments, the packaged `prepare_fresh_run.py`
plan and explicit execution completed. It validated the old process identities
and listener ownership, stopped sender and receiver once through their stop RPCs,
and stopped Zebra with graceful SIGTERM. Observed stop times were approximately
0.128, 0.118 and 0.207 seconds. The old `/home/outbox/regtest` directory, wallet
data, Outbox claims and experiment evidence were preserved; no owner was released.

The helper created the distinct `/home/outbox/final-run` root, reused the existing
official binaries through a bin symlink, and successfully ran the packaged
bootstrap's `--verify-existing` mode plus the one-shot configuration/funding
recipe. Each reused executable was compared with its payload inside the
checksum-pinned official archive. This was a new chain/wallet setup within the
existing VM, using retained downloads; it was not a fresh host download or a new
VM provisioning test.

The new chain generated 107 setup blocks. Funding operation
`aed1e58687bad21b7169e12332848e0deeb99d4be69a23352883dbefe7ca5dc8`
succeeded; the recorded private balance was `6.24985000` synthetic ZEC. Both
wallets had publication disabled after the isolated funding transition. The
actual registered address indices were **sender 1** and **receiver 4609798954**.
Both private registrations used their actual matching `z_listaccounts(true)`
entries and creation responses. Assuming zero would have been wrong for both.

[Fresh completion record](../integration/evidence/fresh-environment.json) and
[redacted recipe/binary/funding metadata](../integration/evidence/fresh-runtime-funding.json)
retain software/source hashes, graceful-stop observations and setup snapshots,
without spending/viewing material, credentials or addresses. The historical pre-correction service
suite used this environment; its recorded outcomes are separate in
[acceptance status](acceptance-status.md) and
[historical fresh service evidence](../integration/evidence/real-stack-final/).

## Reproduce

On x86_64 Linux, no Rust build is required for these exact official artifacts:

```bash
bash integration/bootstrap-linux.sh --root "$HOME/outbox"
python3 integration/preflight.py --bin-dir "$HOME/outbox/bin"
python3 integration/fund-release-regtest.py --root "$HOME/outbox"
```

The funding script creates a new `regtest` directory and refuses any existing
one. It fsyncs each RPC request/result, never automatically retries a mutating
RPC, and leaves an uncertain operation for inspection. Read-only polling is
bounded. It uses the explicit temporary-publication funding transition described
above, before any business outbox PCZT. For a Linux directory other than
`/home/outbox`, pass its absolute path consistently.

The funding script records both account/address creation responses, matches each
registered UA against its actual `z_listaccounts(true)` entry, and retains that
entry's actual diversifier index in the private viewing registration. An index
of zero is not assumed. Both registrations and a private RPC credential file are
listed in `regtest/ready.json`. The live adapter independently checks each genuine
created PCZT against its registration before proving or signing.

On Windows, Python 3.11+ and the existing native OpenSSH/curl are sufficient for
the workspace-only VM helper:

```powershell
python integration/portable_vm.py --root work/stack/vm fetch
python integration/portable_vm.py --root work/stack/vm prepare
python integration/portable_vm.py --root work/stack/vm start
python integration/portable_vm.py --root work/stack/vm status
python integration/portable_vm.py --root work/stack/vm ssh
```

Wait for SSH, copy this project's `integration/` sources into the guest and run
the Linux commands there. Keep node/wallet/SQLite state on the guest's ext4 disk;
do not substitute a Windows shared folder. The helper's `tunnel` operation opens
the tested loopback forwards; `stop` requests a graceful guest shutdown. It does
not stop unrelated host processes. Regenerate disposable credentials/keys for a
fresh run; do not copy the task's private work files into a submission.

For a separate clean chain after a quarantined experiment, copy
`prepare_fresh_run.py`, `bootstrap-release-linux.py`, `bootstrap_registration.py`,
`configure-regtest.py`, `fund-release-regtest.py` and `pinned-sources.json` together
into a private guest tools directory. `python3 prepare_fresh_run.py` prints a
read-only plan by default. It verifies owned PID/command/start ticks and listener
socket ownership, plus release binaries against the checksum-pinned archives.
Only after the operator has stopped host Outbox/proxy mutators, its explicit
`--execute` gracefully stops the old wallets and node, preserves their old state
and claims, and provisions the new sibling `/home/outbox/final-run` with a bin
symlink to the verified existing binaries. It calls the packaged bootstrap's
`--verify-existing` mode and the one-shot funding script. It never force-kills,
deletes or moves old wallet data, releases claims, or automatically retries a
failed preparation. Keep the preparation journal private and inspect any
uncertainty instead of rerunning it.

The packaged graceful-stop, verified-binary reuse, configuration and one-shot
funding route was actually executed in the fresh-root rehearsal above. The
download/unpack/VM route was observed earlier. A single combined rerun starting
from an entirely fresh host download and new VM has not been performed.
Preserve those separate scopes when reporting reproducibility.

## Fault infrastructure and remaining scope

`fault_proxy.py` forwards an upstream call once, fsyncs the completed upstream
response digest, then can hold/drop the caller response. It records HTTP completion
separately from RPC error/acceptance. Its loopback HTTP tests are infrastructure
checks, not a substitute for actual Zallet/Zebra fault experiments. Use the
repository acceptance evidence for the actual business-payout outcomes.

The new corrected run passed **all nine scenarios plus genuine identical-intent A/B recovery**, using a new chain/wallet pair in `/home/outbox/corrected-run-001` and the actual verifier. It reused verified official binaries; the old 622 state files were unchanged. Actual registration indices were sender `0` and receiver `4609838961`. New scenario 8 covers application restart/remote completion, not a wallet-process restart. Terminal wallets remain quarantined. [Corrected report](corrected-real-stack-acceptance.md), [new summary](../integration/evidence/real-stack-corrected-001/summary.json), [fresh setup](../integration/evidence/corrected-fresh-environment.json).

The historical pre-correction fresh-run summary records **all nine required
service scenarios passed** against its recorded service/helper/lockfile/orchestrator set, with no
mock wallet or node. It includes mined receiver verification, same-byte recovery
after lost node acceptance, identity/concurrency checks, the durable business
application, actual rejected final COMMIT, extraction response-loss quarantine,
restart/remote completion and corruption rejection. Terminal cases retain both
disposable wallet quarantines. [Final summary](../integration/evidence/real-stack-final/summary.json),
[tested hashes](../integration/evidence/real-stack-final/tested-artifacts.json).

The separate historical pre-correction controlled-reorg experiment also passed: one explicit invalidation
and reconsideration produced reference-app paid observations `true -> false ->
true` and restored the original chain. It made zero wallet, construction or
transaction-submission calls; business bindings, owners, bytes, artifacts and
submission attempts stayed unchanged. The invalidated lookup was `unknown`,
which was not itself classified as proof of reversal; the experiment retained
the positive node-control receipt. [Controlled-reorg summary](../integration/evidence/controlled-reorg/summary.json).

The environment no longer blocks that work. The subsequent bounded wallet
pipeline, independent effects/authorization verification, repeated extraction,
caller-death observations and controlled receiver payment are recorded in
[wallet qualification](qualification-wallet.md). The full durable service path
and application scenarios are assessed separately in the acceptance report.
NU7 compatibility, an enforced verified-restore gate, general uncertain
prove/sign/extract reconciliation, actual expiry/rejection qualification and
external review remain release work. The prior saved-CREATE experiment is historical pre-correction evidence.
[recovery.md](recovery.md) specifies the corrected durable-binding contract and
corrected local and genuine A/B recovery scope; legacy unbound evidence is not eligible, and ownership
is never released or stolen to escape uncertainty.
