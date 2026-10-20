# Contributing

This project follows the relevant [librustzcash contribution and style guidance](https://github.com/zcash/librustzcash/blob/main/CONTRIBUTING.md#styleguides), adapted to this standalone Rust service. Changes to upstream repositories must follow those repositories' own rules.

## Changes and review

- Open an issue describing the affected user workflow or correctness invariant before substantial changes.
- Keep commits focused, document public API changes, and submit a pull request with the problem, final behavior, compatibility impact and validation.
- Require review before merging payment-state, ownership, verification, migration or publication changes. Retain review history and avoid rewriting a branch while it is under active review.
- Use Rust formatting and idiomatic, explicit error handling. Document assumptions and supported transaction shapes. Prefer a demonstrated failing regression followed by its fix for safety-related bugs.

## Required checks

Use Rust 1.95.0 for the currently pinned prototype:

```sh
cargo +1.95.0 fmt --check
cargo +1.95.0 test --locked
cargo +1.95.0 clippy --locked --all-targets -- -D warnings
cargo +1.95.0 build --locked
cargo +1.95.0 build --locked --release --manifest-path verifier/Cargo.toml
python integration/test_infrastructure.py
python integration/test_reference_app.py
python tests/http_smoke.py --binary target/debug/zcash-durable-outbox
```

Use the `.exe` service path on Windows. Replay the preserved baseline with `python scripts/replay_baseline.py`; it runs a temporary synchronized copy and must not modify the original model. Run checks appropriate to the affected component, including verifier checks for verifier changes.

## Payment and evidence invariants

- Commit the original payment/owner/operation/intent binding before CREATE. Never infer it for legacy evidence.
- Retain uncertain wallet-operation fences. A timeout is not cancellation, rollback or permission to clear ownership.
- Commit final transaction bytes before publication. Never reconstruct or re-extract after that commitment.
- Keep the current default publication gate and supported profile explicit. Restore freshness, wallet authority and additional network profiles require their own qualification.
- Identify the exact sources, binaries and harness used for real-stack evidence. Label local fixtures separately and preserve historical reports. Regenerate a publication manifest after packaging changes without rewriting a frozen qualification record.
- Never commit spending/viewing keys, credentials, wallet databases, private PCZTs, complete sensitive transaction artifacts, or runtime images.

Read README.md and the acceptance, recovery and qualification documents before running a real stack. Use fresh disposable regtest state and synthetic credentials; terminal test quarantines must remain intact. Report a suspected vulnerability privately to a repository maintainer before publishing sensitive details or an exploit.
