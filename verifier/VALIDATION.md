# Offline verifier validation

Validated 2026-10-20 against saved artifacts from actual disposable Zallet beta.3
and Zebra regtest processes. The helper performed no RPC, wallet mutation, final
outbox commitment, or publication. The live adapter remains unqualified by this
helper's reports.

Rust 1.95 compilation, optimized build with two jobs, Clippy with warnings denied,
and direct Rustfmt checks passed. The standalone Cargo.lock pins the wallet crates
to librustzcash `1f6bb2072e7fcb142b0d90ff7b267a8699a84818` and Orchard 0.15.3.
Some semver transitive versions differ from Zallet's release lock; the actual saved
proofs passed this helper's independently compiled extractor and batch validator.
Use this exact helper lock when reproducing these results.

All **18 real-fixture checks passed**:

- Created, proven, and signed PCZTs passed effects-only checking with authorization
  explicitly false.
- The signed PCZT and the original wallet-extracted transaction passed independent
  payment-effect, proof, spend-signature, and binding-signature checks.
- The same signed PCZT and a second actual wallet extraction also passed. Their
  independently computed consensus txids match, while their raw hashes differ.
- Changed recipient, amount, memo, absent memo, fee cap, fee rule, privacy, expiry,
  and account were rejected.
- Expiry at the next mining height, unsigned PCZT in completed mode, trailing raw
  bytes, and an altered actual binding signature were rejected.

The real transaction uses five Ironwood actions, one positive sender input, a
100,000-zat recipient payment, four independently authenticated internal change
notes totalling 624,860,000 zat, a 25,000-zat ZIP-317 fee, and expiry height 148.
The supplied node tip was 107. All 512 memo bytes were compared. No display-address
or private `postcard` metadata was used to authorize an output.

Independent consensus txid:

`340ffa32bea903483a0ff40bb166df482be26417960e4cad9cdcb5b60e461718`

Original extracted raw SHA-256:

`a09a9118aa24baa38d226985d892fe4ed0cbb0506c7da6f41b9e2cace284b0d2`

Repeated extraction raw SHA-256:

`28ce05ea2fc9061d080f2c009b9dac06c3157f6d8f6e3470a7fd77dfb26c19b4`

Sanitized per-case results and the executable/lock/artifact hashes are recorded in
`../integration/evidence/real-pczt-verifier/summary.json`. Private viewing keys,
PCZTs, raw transactions, and mutated scratch inputs are intentionally kept out of
that evidence folder. `run_real_fixture.py` reproduces the checks given access to
the disposable artifacts, the trusted bootstrap file, and the built helper.

These results qualify the exercised artifact checks. They do not establish wallet
input reservation, extraction cancellation, lost-response reconciliation, outbox
COMMIT durability, receiver settlement, or all possible zero-output/padding shapes.
In particular, the observed randomized extraction means a later replay must never
replace already-committed bytes. Unsupported shapes or missing fields still fail
closed; no timeout, force switch, or public publication capability is added.
