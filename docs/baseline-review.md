# Request and regression-baseline review

Reviewed 2026-10-20 (America/Toronto).

## Available material

The explicit attachment for this request is:

`C:\Users\NewOwner\.codex\attachments\8f796d0a-734c-4491-ab7e-7c1f7647b9f3\Pasted text.txt`

It is 5,408 bytes and contains the implementation request. An unchanged copy is preserved as `reference/user-request.txt`; its SHA-256 is recorded in `reference/manifest.json`.

The matching feasibility package was located in `C:\Users\NewOwner\Downloads\zcash-payment-outbox-feasibility.zip` (14,458 bytes). A second filename with ` (1)` has the identical SHA-256, `348bcc1c7423f2049d5e3747df5d1be24f1f979e334ed61fea3ad8f7b1fdb317`. Its only three files are preserved unchanged under `reference/zcash-outbox-probe/`: `README.md`, `probe.py`, and `results.json`.

No repository existed in the task directory when inspected. No `AGENTS.md` exists at the workspace or its checked ancestor directories (`C:\`, `C:\Users`, the user directory, `Documents`, `Documents\Codex`, and `Documents\Codex\2026-10-19`). Upstream source checkouts created during this task have their own instructions, which apply when modifying those checkouts.

## Proposal status

The user subsequently supplied the correct `Zcash-Durable-Payment-Outbox-Proposal-and-MVP.docx`, dated 19 October 2026. It was read fully on 20 October and preserved unchanged under `reference/`; its SHA-256 is `a6252bbba41f4b4fe8e1431f6c022029259c8acd5d76909bc6154d29d89828e5`. The missing-proposal blocker is cleared. The previously inspected gateway DOCX concerned another project and remains only historical search evidence.

The explicit build request controls execution. The proposal supplies product scope, intended guarantees and acceptance criteria; its historical research, budget, future funded deliverables and publication/reviewer plans are not independent authorization to spend, publish, contact third parties, or claim completion. See [proposal alignment](proposal-alignment.md) for the comparison with the current code and remaining gaps.

## Preserved regression baseline

The original `results.json` records 15 passing test groups and 35 durability-boundary subprocess-crash cases with Python 3.12.14 and SQLite 3.53.1. This is explicitly a fake-wallet/fake-chain model with no keys, PCZTs, consensus transactions, network calls, or Zcash txid implementation.

The model uses `BEGIN IMMEDIATE`, WAL, `synchronous=FULL`, a per-intent owner, and a singleton wallet pipeline lock. Its durable stages are `create`, `prove`, `sign`, `extract`; every completed output is persisted as the next stage input. A committed stage fence marks unknown in-flight work. Raw fixture bytes, their SHA-256 integrity digest and release of the wallet slot are committed together before publication. The broadcaster reads the committed bytes independently.

The tested paths are `PENDING -> BUILDING -> READY -> SENDING -> ACK`, plus `HALTED` for ambiguity. An ordinary retry returns busy for unresolved ownership. The supervisor-only recovery function requires confirmed local worker death, and the README explicitly warns that it does not qualify a remote RPC continuing after caller death. The `no_broadcast` recovery policy is a stated assumption; the `unknown` policy halts precommit uncertainty and retains the wallet slot.

The preserved 15 groups cover canonical identity and integer values; intent COMMIT/lost acknowledgment; ordinary/completed retry; 12 concurrent same-ID callers; distinct IDs sharing a wallet; slot retention after lost prove response; 12 retries after lost broadcast response; 33 build/send/receipt crash points plus 2 intent points; four lost precommit responses under an assumed nonpublishing adapter; eight unknown-semantics lost/crash cases; real deferred-FK COMMIT rejection; expiry/reorg stops; byte corruption; claim nonstealing; and an intentionally unsafe send-first/save-later counterexample producing two fixture payments.

Two unchanged replays on this host reached a timing race in the distinct-ID concurrency test at `probe.py:475`: the test holds the first worker for only 0.2 seconds, and the second Windows Python process can start after that hold ends. The preserved model was not changed. `scripts/replay_baseline.py` prepares a disposable replay copy that replaces only that test's pause with an explicit entered/release file handshake. The exact synchronization-only diff and the replay result are retained under `evidence/`. This adjustment changes no payment logic or assertions. The original failure is retained as `evidence/baseline-replay.log`.

The synchronized replay passed all 15 groups and 35 actual process-crash boundaries with zero failures on Python 3.11.9 and SQLite 3.45.1. The observed result is `evidence/baseline-replay-synchronized.json`. Reproduce it with `python scripts/replay_baseline.py` from the project directory. This result establishes the preserved model's local durability checks with the documented synchronization adjustment; it provides no real Zallet/Zebra integration evidence.

## Requirements taken directly from the available request

| Area | Required behavior |
| --- | --- |
| Deployment | One host, durable transactional SQLite database, dedicated payout wallet, Rust service, authenticated HTTP API, CLI, bounty/refund example. |
| Supported payment | One shielded single-transaction profile; integer zatoshis and memo bytes; no TEX, multistep payment, distributed worker, or automatic replacement. |
| Identity | Bind a business payment ID to canonical immutable intent including network, wallet/account, recipients, amounts, memos, fee, expiry and privacy policy. Identical requests return existing record; changed intent is rejected. |
| Ownership | Serialize all construction for the entire dedicated wallet. Persist operation markers and ownership. An elapsed lease or timeout must not allow another construction while the remote operation could remain active. |
| Staging | Persist completed PCZT artifacts before advancing. Retry a precommit stage only with qualified adapter semantics. Uncertain unsafe operations require reconciliation with evidence retained. |
| Publication | Inspect actual payment effects. Commit chosen final bytes, integrity digest and real Zcash txid before broadcast. Broadcaster reads the committed durable artifact. |
| Critical retry invariant | After final-byte commitment, every retry uses identical stored bytes and makes zero additional construction calls. |
| Observability | Store submission attempts and wallet/chain observations. Offer submit-or-return, status, unresolved listing and eligible existing-submission retry. |
| Environment | Disposable local-chain sender/receiver wallets and synthetic credentials only. Pin exact compatible Zallet/Zebra/dependencies and network upgrades. |
| Qualification | Exercise create, inspect, prove, sign and extract against genuine processes; determine publication side effects, wallet record/input effects, repeated extraction, lost-response recovery, restart behavior and remote continuation after caller death. Source comments alone are insufficient. |
| Safety gate | If an essential wallet recovery boundary cannot be qualified, retain evidence and document the exact blocker without an unsafe workaround. |

## Required real-stack scenarios

1. Shielded payout, receiver verification, repeat completed request.
2. Drop Zebra acceptance response after action, restart app, resubmit identical bytes.
3. Concurrent retries for one business ID.
4. Reject changed recipients, amounts, memos and policies under existing ID.
5. Two IDs share the wallet without construction/input-selection races.
6. Reject the actual outbox database COMMIT before publication and observe zero outbox submissions.
7. Lost PCZT responses, especially after extraction records the wallet transaction.
8. Wallet/application restarts and remote completion after caller death.
9. Detect a stored-byte integrity mismatch.

The request specifically distinguishes real integration proof from fake wallet/chain operations. Unit tests or synthetic RPC responses can check local state-machine properties but cannot establish any of these real wallet/node semantics.

## Implementation lessons

- A timed-out RPC is an uncertain operation, not proof that the wallet stopped working.
- An extracted wallet transaction and committed application artifact are separate durable facts; response loss can occur between them.
- Recovery evidence must survive application restarts, including retained PCZT bytes and operation identity.
- A successful transaction sent through any wallet background path before the outbox COMMIT would violate the required publication boundary.
- Transaction identity must come from Zcash transaction parsing/consensus logic, not a generic digest of raw bytes. The integrity digest serves a separate purpose.
- The absence of additional construction calls after commitment needs an explicit assertion in failure tests.
