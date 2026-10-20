# Upgrade compatibility and execution options

Reviewed 2026-10-20 (America/Toronto). Upgrade conclusions below are source
inspection; the later VM and funding observations are reported separately.

## The proposal's release gate remains open

The supplied proposal, dated 19 October 2026, paragraph 25 says:

> Compatibility. Start with one demonstrated shielded single-transaction profile
> and publish exact wallet, node, network and upgrade versions. NU7 compatibility
> is a release gate. Zallet PR 898 was still open and draft when checked for this
> proposal; beta.3 must not be advertised as NU7-qualified. [4]

The PR 898 statement is the proposal's historical observation. This report does
not assert its current status and did not check or depend on that status. See
[proposal-alignment.md](proposal-alignment.md) for the original document's provenance.

**The generated NU6.3 regtest profile does not meet the NU7 release gate.** It can
be a bounded prototype qualification target, but cannot close that release condition.
Actual environment/funding and business-payout results do not establish a later upgrade.

## What the exact pins implement

The original source assessment is restricted to Zallet
`e5c16c464d2d641e5a7c386f3682cfa37e1c43f9`, its patched librustzcash revision
`1f6bb2072e7fcb142b0d90ff7b267a8699a84818`, and Zebra v6.2.3
`7121c82ba6795a151523d5b308a19743f4a1ade7`. It makes no claim about newer releases.
The later executed runtime uses official Zallet beta.3 release commit
`987382f67e622915228686e9f956c6a9c9a7514c` with its `zaino` JSON-RPC backend.
That release also gates NU7 behind `zcash_unstable="nu7"`; its observed build
recipe does not enable that cfg. Successful NU6.3 funding is not NU7 evidence.
[Release network definition](https://github.com/zcash/zallet/blob/987382f67e622915228686e9f956c6a9c9a7514c/zallet-core/src/network.rs),
[release AMD64 build recipe](https://github.com/zcash/zallet/blob/987382f67e622915228686e9f956c6a9c9a7514c/Dockerfile.stagex).

| Executable code or configuration | Finding | Consequence |
|---|---|---|
| Zallet upgrade list/local network | `BranchId::Nu7` and its activation field are guarded by `zcash_unstable = "nu7"`. | The default build is not a NU7 build. |
| Initially inspected Zallet Zebra regtest conversion | Copies heights through NU6.3 but sets `nu7: None`. | A config NU7 entry alone would not reach that state view; this is not the selected release's pure-RPC mode. |
| librustzcash main/test consensus parameters | NU7 is cfg-gated; its public-network activation methods return `None`. | These sources do not schedule NU7 on those networks. |
| librustzcash branch conversion | Experimental NU7 accepts/emits placeholder `0xffffffff`. | A variant named Nu7 is not production branch-ID compatibility. |
| librustzcash transaction version logic | Experimental NU7 selects `TxVersion::V6` and has cfg-gated validity branches. | Partial executable NU7 work exists; it is not a qualified complete stack. |
| Zebra branch map | NU7 entry is test/`zebra-test` only, using placeholder `0xfffffffe`; main/test activation arrays end at NU6.3. | This production node branch map does not provide a production NU7 pairing with the wallet's experimental placeholder. |
| This project's bootstrap/config | No `zcash_unstable="nu7"` cfg; NU6.3 at height 2; no NU7 activation. | The recipe is expressly NU6.3. |

Exact primary source references:

- [Zallet upgrade list and local activation construction](https://github.com/zcash/zallet/blob/e5c16c464d2d641e5a7c386f3682cfa37e1c43f9/zallet-core/src/network.rs#L23).
- [Zallet regtest conversion with `nu7: None`](https://github.com/zcash/zallet/blob/e5c16c464d2d641e5a7c386f3682cfa37e1c43f9/backends/zebra/src/read_state.rs#L110).
- [librustzcash main/test activation parameters](https://github.com/zcash/librustzcash/blob/1f6bb2072e7fcb142b0d90ff7b267a8699a84818/components/zcash_protocol/src/consensus.rs#L480).
- [librustzcash experimental branch conversion](https://github.com/zcash/librustzcash/blob/1f6bb2072e7fcb142b0d90ff7b267a8699a84818/components/zcash_protocol/src/consensus.rs#L744).
- [librustzcash transaction versions](https://github.com/zcash/librustzcash/blob/1f6bb2072e7fcb142b0d90ff7b267a8699a84818/zcash_primitives/src/transaction/mod.rs#L192).
- [Zebra conditional branch map](https://github.com/ZcashFoundation/zebra/blob/7121c82ba6795a151523d5b308a19743f4a1ade7/zebra-chain/src/parameters/network_upgrade.rs#L230).
- [Zebra main/test activation arrays](https://github.com/ZcashFoundation/zebra/blob/7121c82ba6795a151523d5b308a19743f4a1ade7/zebra-chain/src/parameters/network_upgrade.rs#L103).

Zebra's nearby comment names a possible future branch ID; a TODO comment is not
an implemented consensus identity. The decisive findings are the conditional map
entry and the backend's `nu7: None`. Zallet's compatibility checker treats unknown
active/pending branches or mismatched heights as incompatibilities. Bypassing it
would not establish correct chain interpretation.
[Compatibility implementation](https://github.com/zcash/zallet/blob/e5c16c464d2d641e5a7c386f3682cfa37e1c43f9/zallet-core/src/components/chain.rs#L342).

Closing the gate requires refreshing and pinning a wallet/node cohort that agrees
on the required NU7 rules, branch identity and activation schedule, then executing
the funded single-transaction profile, payment-effects verifier, extraction/recovery
experiments and decisive faults under that profile. Default-build tests or an
unstable cfg alone cannot replace that evidence. A different upgrade requires a
new qualification report.

## Linux execution was subsequently established

The initial read-only inventory found no available QEMU/VirtualBox/Podman/Multipass
or WindowsSandbox executable, while the original host had no WSL or Docker. That
inventory did not prove Linux execution impossible. Research identified portable
QEMU with a disposable Linux disk as a reversible route.
[QEMU Windows downloads](https://www.qemu.org/download/#windows),
[system emulation](https://www.qemu.org/docs/master/system/introduction.html).

The subsequent task downloaded and hash-verified exact QEMU, extraction-tool and
Ubuntu image archives into `work/`; installers were unpacked, never executed.
The host's already-available WHPX accelerator worked with
`pc,kernel-irqchip=off`, `qemu64`, four vCPUs and 6 GiB RAM. Linux SSH, actual
release Zallet/Zebra processes and synthetic Ironwood funding then succeeded.
TCG was not used. No Windows feature, kernel driver or host OS installation was
needed. All wallet/node/SQLite state stays on the guest's ext4 disk; only loopback
SSH and SSH RPC forwards cross the host boundary.
[Actual stack qualification and evidence](qualification-stack.md),
[QEMU WHPX documentation](https://www.qemu.org/docs/master/system/whpx.html).

This clears the environment blocker. It does not clear the independent NU7,
extraction, payment-verification or complete-service qualification gates.
