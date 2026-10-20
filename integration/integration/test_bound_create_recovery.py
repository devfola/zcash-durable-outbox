"""Genuine bound-CREATE recovery, called before the nine scenarios on the SAME DB.

Import run_bound_create_recovery(h, primary_db) from test_real_stack.py. This
module has no standalone execution path. It forwards real RPCs only when that
reviewed runner executes with include_bound_create_recovery=true. It never edits
payment/owner/wallet state, replays CREATE, or adopts historical unbound evidence.
The diagnostic artifact-insert trigger is confined to the disposable Outbox DB.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

from qualification_artifacts import artifact_hashes

A = "bound-create-origin-A"
B = "bound-create-origin-B"
TRIGGER = "qualification_bound_create_artifact_rejection"
DIAGNOSTIC = "qualification: B created-artifact transaction rejected"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def readonly(database):
    return sqlite3.connect(f"file:{Path(database).resolve().as_posix()}?mode=ro", uri=True, timeout=30)


def operation_snapshot(database, payment_id):
    with readonly(database) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        payment = db.execute("SELECT id,intent,intent_digest,stage,inflight,owner,raw,txid FROM payments WHERE id=?", (payment_id,)).fetchone()
        require(payment is not None, "bound recovery payment missing")
        binding = db.execute("SELECT operation_id,payment_id,owner,intent_digest FROM create_operations WHERE payment_id=?", (payment_id,)).fetchone()
        require(binding is not None, "original durable CREATE binding missing")
        held = db.execute("SELECT payment FROM wallet WHERE id=1").fetchone()[0]
        return dict(payment), dict(binding), held


def logical_digest(database):
    """Hash logical rows, including BLOB hashes, without exporting private rows."""
    snapshot = {}
    with readonly(database) as db:
        db.execute("BEGIN")
        for table in ("wallet", "payments", "create_operations", "artifacts", "attempts", "events"):
            rows = db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            snapshot[table] = [[{"sha256": sha(value), "size": len(value)} if isinstance(value, bytes) else value
                                for value in row] for row in rows]
    return sha(canonical(snapshot).encode())


def methods(common, g):
    def calls(path):
        return Counter(item.get("method") for item in common.entries(path) if item.get("event") == "forward_started")
    return {"wallet": calls(g.wallet_journal), "node": calls(g.node_journal)}


def method_delta(before, after):
    require(all(after[k][m] >= before[k][m] for k in before for m in before[k]), "RPC journal counter moved backwards")
    return {key: dict(after[key] - before[key]) for key in before}


def payment_only(state):
    # db_state includes the shared wallet slot, which legitimately moves from A
    # to B even though A's own immutable payment state must stay unchanged.
    return {key: value for key, value in state.items() if key != "wallet_owner"}


def save_private(path, data):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def cli_reconcile(h, g, live_config, payment_id, response, label):
    require(h.process is None, "operator CLI requires owned service stopped")
    command = [h.config["service"], "--db", str(g.database.resolve()),
        "--wallet", g.trusted["wallet"], "--account", g.trusted["account"],
        "--live-config", str(live_config.resolve()), "reconcile-create", payment_id,
        "--response", str(response.resolve())]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    completed = subprocess.run(command, capture_output=True, timeout=900,
        env=h.environment, creationflags=flags)
    save_private(g.runtime / (label + "-stdout.json"), completed.stdout)
    save_private(g.runtime / (label + "-stderr.json"), completed.stderr)
    return completed


def source_artifacts(root, binding, pre_dispatch):
    directory = root / ("create-" + binding["operation_id"])
    require(directory.resolve().parent == root.resolve(), "CREATE source left original adapter root")
    files = {}
    for name in ("binding.json", "intent.json", "request.json", "response.json", "completed.pczt"):
        path = directory / name
        require(path.is_file() and not path.is_symlink() and path.resolve().parent == directory.resolve(), "CREATE source file absent or redirected")
        files[name] = path.read_bytes()
    require(json.loads(files["binding.json"]) == binding, "source binding differs from original durable binding")
    require(sha(files["binding.json"]) == pre_dispatch["binding_json_sha256"], "original pre-dispatch binding changed")
    response = json.loads(files["response.json"])
    require(isinstance(response, dict) and "error" not in response and isinstance(response.get("pczt"), str), "missing successful genuine CREATE result")
    require(base64.b64decode(response["pczt"], validate=True) == files["completed.pczt"], "saved response PCZT differs from completed artifact")
    request = json.loads(files["request.json"])
    require(request.get("method") == "pczt_create", "source is not the genuine CREATE operation")
    reports = []
    for path in directory.glob("verification-*/verifier-stdout.json"):
        value = json.loads(path.read_bytes())
        if value.get("artifact_effects_verified") is True and value.get("pczt_sha256") == sha(files["completed.pczt"]):
            reports.append(value)
    require(len(reports) == 1, "genuine CREATE lacks its actual independent verifier report")
    require(reports[0].get("cryptographic_authorization_verified") is False
            and reports[0].get("raw_response_verified") is False, "created artifact incorrectly claims completed authorization")
    return directory, files, reports[0]


def final_verification(root, state):
    reports = []
    for path in root.glob("extract-*/verification-*/verifier-stdout.json"):
        report = json.loads(path.read_bytes())
        if report.get("raw_sha256") == state["byte_digest"] and report.get("consensus_txid") == state["txid"]:
            reports.append(report)
    require(len(reports) == 1, "final real bytes lack unique independent verification report")
    report = reports[0]
    require(all(report.get(key) is True for key in ("artifact_effects_verified", "authorization_verified",
        "cryptographic_authorization_verified", "raw_response_verified")), "actual final authorization not independently verified")
    return {key: report[key] for key in ("artifact_effects_verified", "authorization_verified",
        "cryptographic_authorization_verified", "raw_response_verified", "consensus_txid", "raw_sha256", "pczt_sha256", "effects")}


def run_bound_create_recovery(h, primary_db):
    """Return a redacted supplement; leave A/B accepted on the same released DB."""
    # Runtime import avoids a circular top-level dependency when the main runner
    # imports this function; shared helpers create no side effects on import.
    import test_real_stack as common

    initial_hashes = artifact_hashes(h.config, orchestrator=Path(__file__).resolve())
    evidence = h.evidence / "bound-create-recovery"
    evidence.mkdir()
    common.write_json(evidence / "tested-artifacts.json", initial_hashes)
    require(not h.records, "bound recovery must precede the nine scenario records")
    try:
        with common.group(h, "bound-create-recovery", h.config["primary"], primary_db) as g:
            h.start(g)
            live_config = g.runtime / "live-0.json"
            adapter_root = g.runtime / "adapter-0"
            require(g.launches == 1, "A/B must share their original adapter evidence root")
            with readonly(primary_db) as db:
                require(db.execute("SELECT count(*) FROM payments").fetchone()[0] == 0,
                        "genuine bound recovery requires this new dedicated outbox DB")
            # The app is idle; only this B-specific diagnostic trigger is added.
            # A completes normally. A failed B artifact insert rolls back the
            # whole finish_stage transaction, retaining its original CREATE fence.
            with sqlite3.connect(primary_db) as db:
                db.execute(f"CREATE TRIGGER {TRIGGER} BEFORE INSERT ON artifacts "
                    f"WHEN NEW.payment='{B}' AND NEW.stage='created' "
                    f"BEGIN SELECT RAISE(ABORT,'{DIAGNOSTIC}'); END")
            approved = h.intent(g.trusted)
            h.register(A, approved)
            h.register(B, approved)

            pre_dispatch = {}
            hook_lock = threading.Lock()
            original_record = g.wallet_proxy.record

            def observe_before_dispatch(event, **details):
                if event == "forward_started" and details.get("method") == "pczt_create":
                    # FaultServer invokes record before its single upstream call.
                    # Any failure here happens before forwarding, never afterward.
                    with hook_lock:
                        expected = (A, B)[len(pre_dispatch)] if len(pre_dispatch) < 2 else None
                        require(expected is not None, "unexpected extra genuine CREATE dispatch")
                        payment, binding, held = operation_snapshot(primary_db, expected)
                        require(payment["stage"] == "pending" and payment["inflight"] == "create"
                                and payment["owner"] == binding["owner"] and held == expected
                                and binding["payment_id"] == expected and payment["raw"] is None
                                and payment["txid"] is None, "original CREATE fence not durable before dispatch")
                        require(binding["intent_digest"] == payment["intent_digest"] == sha(payment["intent"].encode()),
                                "original intent digest not durable before dispatch")
                        path = adapter_root / ("create-" + binding["operation_id"]) / "binding.json"
                        require(path.is_file() and not path.is_symlink(), "original binding file absent before dispatch")
                        raw_binding = path.read_bytes()
                        require(json.loads(raw_binding) == binding, "binding.json not original before dispatch")
                        observation = {"payment_id": expected, "operation_id": binding["operation_id"],
                            "owner_sha256": sha(binding["owner"].encode()), "intent_digest": binding["intent_digest"],
                            "binding_json_sha256": sha(raw_binding), "fresh_readonly_database_snapshot": True,
                            "proxy_call": details.get("call"), "rpc_request_sha256": details.get("request_sha256"),
                            "rpc_request_size": details.get("request_size"),
                            "observed_before_upstream_dispatch": True}
                        common.write_json(evidence / (expected + "-before-dispatch.json"), observation)
                        pre_dispatch[expected] = observation
                original_record(event, **details)

            g.wallet_proxy.record = observe_before_dispatch
            print(json.dumps({"progress": "genuine_bound_create", "payment": "A"}), flush=True)
            h.retry(A)
            observed_a = h.observe(A, approved, g)
            a_before = common.db_state(primary_db, A)
            payment_a, binding_a, held = operation_snapshot(primary_db, A)
            require(held is None and a_before["stage"] == "accepted", "A did not release its wallet normally")
            source_a, files_a, created_report_a = source_artifacts(adapter_root, binding_a, pre_dispatch[A])
            full_report_a = final_verification(adapter_root, a_before)
            counts_after_a = methods(common, g)

            print(json.dumps({"progress": "genuine_bound_create", "payment": "B", "fault": "artifact_insert_abort"}), flush=True)
            status, failure = h.api.call("POST", "/payments/" + B + "/retry")
            require(status == 500 and DIAGNOSTIC in failure.get("error", ""), "B did not reach the diagnostic real artifact transaction rejection")
            h.stop()
            b_before = common.db_state(primary_db, B)
            payment_b, binding_b, held = operation_snapshot(primary_db, B)
            require(payment_a["intent"] == payment_b["intent"] and binding_a["intent_digest"] == binding_b["intent_digest"],
                    "A/B approved intents differ")
            require(binding_a["operation_id"] != binding_b["operation_id"] and binding_a["payment_id"] != binding_b["payment_id"],
                    "A/B were not distinct durable original operations")
            require(b_before["stage"] == "pending" and b_before["inflight"] == "create"
                    and b_before["owner"] == binding_b["owner"] and held == B
                    and b_before["raw"] is None and b_before["txid"] is None and not b_before["attempts"],
                    "diagnostic rejection changed B's original fence")
            require(set(pre_dispatch) == {A, B}, "both actual CREATE dispatches were not observed")
            source_b, files_b, created_report_b = source_artifacts(adapter_root, binding_b, pre_dispatch[B])
            require(files_a["intent.json"] == files_b["intent.json"] and files_a["request.json"] == files_b["request.json"],
                    "genuine A/B requests and approved details were not identical")
            delta = method_delta(counts_after_a, methods(common, g))
            require(delta["wallet"].get("pczt_create") == 1 and all(delta["wallet"].get(m, 0) == 0
                    for m in ("pczt_prove", "pczt_sign", "pczt_extract")) and delta["node"].get("sendrawtransaction", 0) == 0,
                    "B diagnostic boundary ran another construction/publication stage")
            # Remove only the diagnostic trigger while the owned service is
            # stopped. This cannot release a claim or alter any payment row.
            before_drop = logical_digest(primary_db)
            with sqlite3.connect(primary_db) as db:
                db.execute(f"DROP TRIGGER {TRIGGER}")
            require(logical_digest(primary_db) == before_drop, "removing diagnostic trigger changed business/ownership rows")

            before_cross = logical_digest(primary_db)
            before_cross_calls = methods(common, g)
            rejected = cli_reconcile(h, g, live_config, B, source_a / "response.json", "reject-A-for-B")
            require(rejected.returncode != 0, "A's actual response was accepted for B")
            rejection = json.loads(rejected.stderr)
            require("saved CREATE evidence" in rejection.get("error", ""), "cross-adoption failed outside the expected origin guard")
            require(methods(common, g) == before_cross_calls, "cross-adoption rejection reached RPC")
            require(logical_digest(primary_db) == before_cross, "cross-adoption rejection changed durable rows")
            require(payment_only(common.db_state(primary_db, A)) == payment_only(a_before)
                    and common.db_state(primary_db, B) == b_before,
                    "cross-adoption rejection changed A or B")

            before_recover_calls = methods(common, g)
            recovered = cli_reconcile(h, g, live_config, B, source_b / "response.json", "recover-original-B")
            require(recovered.returncode == 0, "B's genuine original bound response was rejected")
            recovered_payment = json.loads(recovered.stdout)
            require(recovered_payment.get("id") == B and recovered_payment.get("stage") == "created"
                    and recovered_payment.get("inflight") is None and recovered_payment.get("owner") == b_before["owner"],
                    "B adoption did not retain original identity/owner")
            _, original_b, held = operation_snapshot(primary_db, B)
            require(original_b == binding_b and held == B, "adoption changed original operation or wallet slot")
            with readonly(primary_db) as db:
                stored_pczt = db.execute("SELECT bytes FROM artifacts WHERE payment=? AND stage='created'", (B,)).fetchone()[0]
            require(stored_pczt == files_b["completed.pczt"], "adoption stored different genuine PCZT bytes")
            recovery_delta = method_delta(before_recover_calls, methods(common, g))
            require(recovery_delta == {"wallet": {"pczt_inspect": 1}, "node": {"getblockchaininfo": 1, "getblockhash": 1}},
                    "recovery used an unexpected RPC or replayed CREATE")
            reconciliations = list(adapter_root.glob("reconcile-create-*/audit.json"))
            require(len(reconciliations) == 1, "genuine recovery audit missing or duplicated")
            recovery_audit = json.loads(reconciliations[0].read_bytes())
            require(recovery_audit.get("operation_binding") == binding_b
                    and recovery_audit.get("pczt_sha256") == sha(files_b["completed.pczt"])
                    and recovery_audit.get("source_binding_sha256") == sha(files_b["binding.json"]),
                    "recovery audit not tied to original genuine B evidence")

            calls_before_resume = g.calls()
            h.start(g)
            h.retry(B)
            b_accepted = common.db_state(primary_db, B)
            require(b_accepted["txid"] != a_before["txid"], "distinct obligations share one committed txid")
            require(g.calls()["pczt_create"] == calls_before_resume["pczt_create"] == 2,
                    "completed recovery replayed CREATE")
            require(all(g.calls()[method] - calls_before_resume[method] == 1
                    for method in ("pczt_prove", "pczt_sign", "pczt_extract")), "B did not continue exactly once from its saved PCZT")
            observed_b = h.observe(B, approved, g)
            final_b = common.db_state(primary_db, B)
            require(final_b["wallet_owner"] is None and final_b["inflight"] is None and final_b["stage"] == "accepted",
                    "B did not release its wallet through normal final commitment")
            require(common.db_state(primary_db, A) == a_before, "B continuation altered A")
            full_report_b = final_verification(g.runtime / "adapter-1", final_b)
            require(g.calls() == {"pczt_create": 2, "pczt_prove": 2, "pczt_sign": 2, "pczt_extract": 2},
                    "A/B did not use exactly two genuine construction pipelines")
            require(len(g.submissions()) == 2, "A/B did not publish exactly their two committed transactions")
            require(artifact_hashes(h.config, orchestrator=Path(__file__).resolve()) == initial_hashes,
                    "executable/source/harness artifacts changed during bound recovery experiment")
            report = {"passed": True, "real_wallet_and_verifier": True, "same_database_and_wallet_as_nine_scenarios": True,
                "synthetic_pczt_or_verifier": False, "diagnostic_fault": "B-specific Outbox artifact INSERT abort",
                "trigger_removed_only_while_service_stopped": True, "owner_or_payment_rows_manually_edited": False,
                "same_approved_intent_digest": binding_b["intent_digest"], "pre_dispatch_binding_observations": pre_dispatch,
                "source_response_sha256": {"A": sha(files_a["response.json"]), "B": sha(files_b["response.json"])},
                "source_pczt_sha256": {"A": sha(files_a["completed.pczt"]), "B": sha(files_b["completed.pczt"])},
                "created_effects_verified": {"A": created_report_a["artifact_effects_verified"], "B": created_report_b["artifact_effects_verified"]},
                "cross_adoption_rejected_before_rpc": True, "cross_adoption_state_unchanged": True,
                "B_original_response_recovered_under_same_owner": True, "recovery_rpc_delta": recovery_delta,
                "CREATE_replays": 0, "construction_calls": g.calls(), "node_submissions": len(g.submissions()),
                "A": observed_a, "B": observed_b, "independent_final_verification": {"A": full_report_a, "B": full_report_b},
                "wallet_released_by_normal_commit": True, "manual_quarantine_bypass": False,
                "tested_artifacts": initial_hashes, "private_artifacts_in_report": False}
            common.write_json(evidence / "summary.json", report)
            print(json.dumps({"supplement": "bound-create-recovery", "passed": True}), flush=True)
            return {"passed": True, "evidence": "bound-create-recovery/summary.json",
                "real_wallet_and_verifier": True, "same_primary_database": True,
                "cross_adoption_rejected_before_rpc": True, "CREATE_replays": 0,
                "A_txid": observed_a["txid"], "B_txid": observed_b["txid"]}
    except BaseException as error:
        common.write_json(evidence / "aborted.json", {"passed": False, "error_type": type(error).__name__,
            "automatic_replay_or_owner_release": False,
            "instruction": "Preserve this same DB and wallet; inspect durable claims. Do not switch DBs or rerun mutations."})
        raise


if __name__ == "__main__":
    raise SystemExit("Import this module from the reviewed real-stack runner; no standalone wallet execution is provided.")
