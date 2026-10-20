# Bounty and refund reference application

`examples/bounty_refund.py` maintains its own durable SQLite business ledger and calls the authenticated local Rust HTTP API. A bounty uses its original work identifier; a refund uses its original purchase identifier. The payment ID is always `bounty:<business-id>` or `refund:<business-id>`. Keep that ID and the same ledger on every invocation, including after a timeout or restart.

On registration, an immediate SQLite transaction binds the ID to the exact approved intent and confirmation threshold **before any HTTP request**. Object key order is ignored; values remain exact. The ledger uses WAL and `synchronous=FULL`, with SQL triggers protecting the ID, intent, digest and threshold from updates or deletion. The service separately enforces its own canonical intent binding. Use lowercase memo hex and a canonical UUID in the approved intent: the application conservatively rejects a successful response whose structured intent differs from its saved approval, even if the service normalized an equivalent spelling.

The application saves returned Outbox states and a journal of request attempts. It does not maintain or restore a refundable balance, select a new identifier, or construct replacement payments after an error. An unfinished attempt or failed request retains the business binding and last observed state. A fresh query can inspect the original payment without an intent file. Preserve the ledger, its WAL, and the Outbox/wallet state together; replacing the ledger loses the application's local business history.

| Command | API action | Meaning |
|---|---|---|
| `register` | `PUT /payments/{same-id}` | Bind and save the intent. Registration alone does not mean payment. |
| `query` | `GET /payments/{same-id}` | Read the existing payment; no intent argument is needed. |
| `pay` | `POST /payments/{same-id}/retry` | Ask the Outbox to drive that existing payment. Never creates a new business ID. |
| `refresh` | `POST /payments/{same-id}/refresh` | Record a chain observation using the saved confirmation threshold. |

`paid` is true only when a successful, matching payment response contains `chain_observation.state: confirmed`, a matching transaction ID, and an integer confirmation count meeting both the saved business threshold and the observation's threshold. The default is three confirmations; set `--confirmations` at the first registration to choose a threshold from 1 through 100. Subsequent calls use that saved value. Pending, committed, submitted, node-observed, mined below threshold, unknown, and failed requests report `paid: false`. Observations describe the latest recorded chain view; later reorganizations require another refresh and investigation, never automatic balance restoration or a new payment ID.

## Local registration demonstration

Build the executable with `cargo build --locked` using the pinned toolchain. In PowerShell, from the project directory:

```powershell
New-Item -ItemType Directory -Path runtime -Force | Out-Null
python -c "import pathlib,secrets; pathlib.Path('runtime/api-token').write_text(secrets.token_urlsafe(36),encoding='ascii')"
.\target\debug\zcash-durable-outbox.exe --db runtime\demo.db serve --token-file runtime\api-token --listen 127.0.0.1:17840
```

In a second terminal:

```powershell
python examples\bounty_refund.py --token-file runtime\api-token --ledger runtime\business.db register --kind bounty --business-id task-7 --intent examples\intent.json
python examples\bounty_refund.py --token-file runtime\api-token --ledger runtime\business.db query --kind bounty --business-id task-7
```

The bundled `examples/intent.json` contains a synthetic address for registration checks. It is not a spendable destination. The service's default mode disables wallet construction and publication; the commands above retain `stage: pending` and `paid: false`.

## Disposable live profile

The optional live adapter is bounded to one dedicated wallet/account and the exact **regtest NU6.3 V6 Ironwood** profile documented in [stack qualification](qualification-stack.md) and [the verifier design](payment-verifier-design.md). It requires the actual checksum-verified release binaries, funded sender with wallet publication disabled, trusted viewing registration, independently compiled verifier, and protected live configuration. Mainnet, public testnet, and NU7 are outside this profile. NU7 remains a proposal release gate.

Start the service with its actual account UUID and prepared live configuration:

```powershell
.\target\debug\zcash-durable-outbox.exe --db runtime\outbox.db --wallet disposable-sender --account <sender-account-uuid> --live-config runtime\live-config.json serve --token-file runtime\api-token
```

Use a **new business ID only for a genuinely new approved obligation**, and a real validated recipient intent prepared for that disposable environment:

```powershell
python examples\bounty_refund.py --token-file runtime\api-token --ledger runtime\business.db register --kind refund --business-id purchase-42 --intent runtime\approved-refund.json --confirmations 3
python examples\bounty_refund.py --token-file runtime\api-token --ledger runtime\business.db pay --kind refund --business-id purchase-42
python examples\bounty_refund.py --token-file runtime\api-token --ledger runtime\business.db query --kind refund --business-id purchase-42
python examples\bounty_refund.py --token-file runtime\api-token --ledger runtime\business.db refresh --kind refund --business-id purchase-42
```

`--ledger` defaults to `runtime/business.db`. `--url` accepts an HTTP numeric loopback origin. The bearer token is read from its file and never printed. HTTP calls use a 900-second timeout, ignore proxy environment settings, reject redirects, and perform no automatic retry after transport loss. On error, retain the ledger and original ID, then query that ID; the Outbox can require reconciliation for an uncertain precommit wallet operation. The application cannot clear that fence.

## Verification evidence

Run `python integration/test_reference_app.py` for eight local application/SQLite protocol checks: durable binding before HTTP, process restart, immutable SQL identity, intent conflict, wrong-intent responses, dropped replies, confirmation policy, process death during a request, and proxy/redirect behavior. These checks exercise the reference application against an isolated HTTP protocol harness; they do not simulate wallet or chain success. [Recorded result](../integration/evidence/reference-app-tests.json).

The corrected fresh Zallet/Zebra suite also passed its reference-app case: both bounty and refund were independently checked at the receiver, wallet stages were serialized, and new application processes queried/paid/refreshed the same IDs and txids after an actual service restart. Confirmed paid observations used the test's configured one-confirmation threshold. The same run passed all nine scenarios and genuine identical-intent A/B recovery with the actual verifier. Scenario 8 covers application restart/remote completion; it did not perform a wallet-process restart. [Corrected reference-app case](../integration/evidence/real-stack-corrected-001/05-two-ids-and-reference-app.json), [corrected report and hashes](corrected-real-stack-acceptance.md).

The historical pre-correction Zallet/Zebra suite passed all nine required scenarios against its recorded build; it does not qualify the corrected CREATE-origin binding. Its reference-app case completed both a bounty and refund with mined receiver verification, serialized wallet stages and a durable business ledger. After the service restarted, new application processes queried, paid again and refreshed those same IDs/txids with confirmed paid observations. [Actual reference-app case](../integration/evidence/real-stack-final/05-two-ids-and-reference-app.json), [historical suite and hashes](../integration/evidence/real-stack-final/summary.json).

The separate historical pre-correction controlled-reorg test passed: explicit node invalidation made the saved payment lookup unknown and the app report paid false; reconsideration restored the original chain and paid true. The transition was true → false → true, with zero wallet/construction/submission calls and unchanged business bindings, owners, committed bytes, artifacts and attempts. Positive node-control receipts establish the controlled experiment; an unknown lookup alone does not prove reversal. No balance credit or new payment ID was created. [Reorg evidence](../integration/evidence/controlled-reorg/summary.json).

`python tests/http_smoke.py` separately exercises the compiled API in disabled mode. The operator-only saved-CREATE recovery is described in [recovery.md](recovery.md); this reference app cannot adopt a response or clear an uncertain prove/sign/extract fence. NU7, enforced verified restore, general extraction reconciliation, actual expiry/rejection qualification and external review remain release work. [Acceptance status](acceptance-status.md).
