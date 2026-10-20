"""Plan-first controlled reorg observation of an EXISTING confirmed bounty.

--execute requires a completed nine-scenario run on the same disposable stack.
Only node invalidateblock/reconsiderblock mutate the chain, each at most once.
No registration, payment retry, construction, adoption, mining, owner release,
wallet RPC, or SQL update is performed by this qualification program. The actual
reference app only refreshes its existing business binding and observation cache.
Reconsideration runs in finally after any attempted invalidation, including an
uncertain invalidation response. Neither uncertain mutation is ever replayed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from qualify_pczt import Rpc, qualified_chain
from test_real_stack import (
    Harness, artifact_hashes, entries, group, load_config, require, sha, write_json,
)


PAYMENT = "bounty:real-stack-award"


def read_connection(path):
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def stable_database_state(path):
    """Exclude only append-only chain observations; retain every ownership fence."""
    with closing(read_connection(path)) as db:
        db.execute("BEGIN")
        wallet = dict(db.execute("SELECT * FROM wallet WHERE id=1").fetchone())
        payments = []
        for row in db.execute("SELECT * FROM payments ORDER BY id"):
            record = dict(row)
            raw = record.pop("raw")
            record["raw_present"] = raw is not None
            record["raw_sha256"] = None if raw is None else sha(raw)
            record["raw_size"] = None if raw is None else len(raw)
            # Preserve the digest binding without copying addresses/memos to this report.
            record["intent_json_sha256"] = sha(record.pop("intent").encode())
            payments.append(record)
        artifacts = [dict(row) for row in db.execute(
            "SELECT payment,stage,length(bytes) AS bytes,digest FROM artifacts ORDER BY payment,stage")]
        attempts = [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")]
        other_events = [dict(row) for row in db.execute(
            "SELECT * FROM events WHERE kind!='chain_observation' ORDER BY id")]
        return {"wallet": wallet, "payments": payments, "artifacts": artifacts,
                "attempts": attempts, "non_observation_events": other_events}


def ledger_bindings(path):
    with closing(read_connection(path)) as db:
        require(db.execute("PRAGMA application_id").fetchone()[0] == 0x424F5842,
                "the exact existing reference business ledger is required")
        return [dict(row) for row in db.execute(
            "SELECT payment_id,intent_json,intent_sha256,required_confirmations,created_at "
            "FROM business_payments ORDER BY payment_id")]


def wait_chain(node, predicate, message, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chain, _ = qualified_chain(node)
        if predicate(chain):
            return chain
        time.sleep(.25)
    raise TimeoutError(message)


def service_refresh(harness):
    status, payment = harness.api.call("POST", "/payments/" + PAYMENT + "/refresh",
                                      {"confirmations": 1}, timeout=60)
    require(status == 200 and payment.get("id") == PAYMENT,
            "existing payment refresh failed")
    observation = payment.get("chain_observation")
    require(isinstance(observation, dict), "service omitted chain observation")
    require(observation.get("txid") == payment.get("txid"), "observation changed transaction identity")
    return payment


def reference_refresh(harness, ledger, evidence, label):
    reference = Path(__file__).resolve().parent.parent / "examples" / "bounty_refund.py"
    command = [sys.executable, str(reference), "--url", harness.api.origin,
               "--token-file", str(harness.token_file), "--ledger", str(ledger),
               "refresh", "--kind", "bounty", "--business-id", "real-stack-award",
               "--confirmations", "1"]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    completed = subprocess.run(command, capture_output=True, timeout=120, creationflags=flags)
    require(completed.returncode == 0, "existing reference-app binding refresh failed")
    result = json.loads(completed.stdout)
    require(result.get("payment_id") == PAYMENT and result.get("http_status") == 200,
            "reference app returned another business identity or an error")
    require(result.get("required_confirmations") == 1,
            "reference app changed the original confirmation threshold")
    write_json(evidence / (label + "-reference-app.json"), result)
    return result


def verify_quarantine(snapshot, payment):
    require(snapshot["wallet"]["payment"] == payment, "terminal wallet-wide quarantine missing")
    row = next((p for p in snapshot["payments"] if p["id"] == payment), None)
    require(row is not None and row["inflight"] == "extract" and row["owner"]
            and not row["raw_present"] and row["txid"] is None,
            "terminal extraction ownership fence missing")


def execute(args, config, plan):
    work, evidence = args.work_dir.resolve(), args.evidence_dir.resolve()
    require(not work.exists() and not evidence.exists(),
            "one-shot work/evidence directories must be fresh; no automatic resume")
    prior_summary_path = Path(config["evidence_dir"]) / "summary.json"
    prior = json.loads(prior_summary_path.read_text(encoding="utf-8"))
    require(prior.get("passed") is True and len(prior.get("scenarios", [])) == 9
            and prior.get("primary_and_secondary_quarantined") is True,
            "a successful, complete nine-scenario run is required first")
    tested = artifact_hashes(config)
    require(prior.get("tested_artifacts") == tested,
            "service/helper/locks/orchestrator differ from the completed run")
    databases = {"primary": args.primary_db.resolve(),
                 "secondary": Path(config["work_dir"]) / "secondary-commit-failure.db"}
    require(all(path.is_file() for path in databases.values()) and args.ledger.is_file(),
            "existing primary/secondary outboxes and exact reference ledger are required")
    before = {label: stable_database_state(path) for label, path in databases.items()}
    verify_quarantine(before["primary"], "terminal-extraction-response-loss")
    verify_quarantine(before["secondary"], "terminal-sqlite-commit-rejection")
    trusted = json.loads(Path(config["primary"]["trusted_wallet"]).read_text())
    require(before["primary"]["wallet"]["name"] == trusted["wallet"]
            and before["primary"]["wallet"]["account"] == trusted["account"],
            "existing outbox is not the configured wallet/account namespace")
    bindings = ledger_bindings(args.ledger)
    bound = next((row for row in bindings if row["payment_id"] == PAYMENT), None)
    require(bound is not None and bound["required_confirmations"] == 1,
            "confirmed bounty binding with its original one-confirmation policy is required")
    with closing(read_connection(args.primary_db)) as db:
        payment = db.execute("SELECT intent,raw,byte_digest,txid,stage FROM payments WHERE id=?",
                             (PAYMENT,)).fetchone()
        require(payment is not None and payment["stage"] == "accepted"
                and payment["raw"] is not None and sha(payment["raw"]) == payment["byte_digest"],
                "existing bounty must retain accepted, intact committed bytes")
        require(json.loads(bound["intent_json"]) == json.loads(payment["intent"]),
                "business ledger and outbox must retain the same exact intent")
        txid = payment["txid"]
    node = Rpc(config["node_url"], None, 30)
    original_chain, genesis = qualified_chain(node)
    tx = node.call("getrawtransaction", [txid, 1])
    block = tx.get("blockhash")
    require(tx.get("txid") == txid and isinstance(block, str) and len(block) == 64
            and type(tx.get("confirmations")) is int and tx["confirmations"] >= 1,
            "real node must currently report this exact transaction as mined")
    header = node.call("getblockheader", [block, True])
    height, tip = header.get("height"), original_chain.get("bestblockhash")
    require(type(height) is int and height > 2 and isinstance(tip, str)
            and 0 <= original_chain["blocks"] - height < 99,
            "selected block must be recent, non-finalized and above pinned activation heights")
    require(node.call("getblockhash", [height]) == block,
            "selected transaction block is not in the active chain")

    work.mkdir(parents=True, mode=0o700)
    evidence.mkdir(parents=True)
    write_json(evidence / "reviewed-plan.json", plan)
    write_json(evidence / "prior-run.json", {"summary_sha256": sha(prior_summary_path.read_bytes()),
        "tested_artifacts": tested, "reorg_script_sha256": sha(Path(__file__).read_bytes()),
        "reference_app_sha256": sha((Path(__file__).resolve().parent.parent /
                                      "examples" / "bounty_refund.py").read_bytes())})
    write_json(evidence / "state-before.json", before)
    write_json(evidence / "initial-node.json", {"genesis": genesis, "chain": original_chain,
        "txid": txid, "transaction_block": block, "transaction_confirmations": tx["confirmations"],
        "transaction_block_height": height})
    harness = Harness(config, work, evidence, node)
    control_attempts = {"invalidateblock": 0, "reconsiderblock": 0}
    restored = False

    def control(method):
        require(control_attempts[method] == 0, "controlled node RPC must never be replayed")
        write_json(evidence / (method + ".started.json"),
                   {"method": method, "params": [block], "time_unix": time.time(), "single_attempt": True})
        control_attempts[method] += 1
        try:
            receipt = node.call(method, [block])
        except BaseException as error:
            write_json(evidence / (method + ".uncertain.json"),
                       {"method": method, "outcome": "uncertain", "error_type": type(error).__name__,
                        "automatic_replay": False})
            raise
        write_json(evidence / (method + ".completed.json"),
                   {"method": method, "params": [block], "result": receipt, "time_unix": time.time()})
        return receipt

    try:
        with group(harness, "reorg-observer", config["primary"], args.primary_db) as observed:
            harness.start(observed)
            confirmed = service_refresh(harness)
            require(confirmed["txid"] == txid
                    and confirmed["chain_observation"].get("state") == "confirmed"
                    and confirmed["chain_observation"].get("blockhash") == block,
                    "service must first confirm the exact existing transaction/block")
            write_json(evidence / "01-confirmed-service.json", confirmed)
            require(reference_refresh(harness, args.ledger, evidence, "01-confirmed").get("paid") is True,
                    "existing reference app must initially report paid")
            invalid_observation = None
            try:
                control("invalidateblock")
                changed = wait_chain(node,
                    lambda chain: chain.get("bestblockhash") != tip and chain["blocks"] == height - 1,
                    "node did not remove the selected block and its descendants")
                write_json(evidence / "02-invalidated-node.json", changed)
                unconfirmed = service_refresh(harness)
                invalid_observation = unconfirmed["chain_observation"]
                require(unconfirmed["txid"] == txid
                        and invalid_observation.get("state") in ("unknown", "node_observed")
                        and invalid_observation.get("confirmations") == 0
                        and invalid_observation.get("blockhash") is None
                        and invalid_observation.get("last_known_mined_block") == block
                        and invalid_observation.get("prior_mining_no_longer_observed") is True,
                        "service retained confirmed settlement after controlled invalidation")
                require(invalid_observation.get("mining_reversed") is
                        (invalid_observation["state"] != "unknown"),
                        "unknown lookup must not be presented as proof of mining reversal")
                write_json(evidence / "02-invalidated-service.json", unconfirmed)
                business = reference_refresh(harness, args.ledger, evidence, "02-invalidated")
                require(business.get("paid") is False,
                        "restarted reference app still reported paid after confirmation loss")
                require({label: stable_database_state(path) for label, path in databases.items()} == before,
                        "observations changed durable owners, committed artifacts or submission attempts")
            except BaseException as error:
                write_json(evidence / "observation-failed.json",
                           {"error_type": type(error).__name__, "error": str(error)})
                raise
            finally:
                if control_attempts["invalidateblock"]:
                    control("reconsiderblock")
                    restored_chain = wait_chain(node,
                        lambda chain: chain.get("bestblockhash") == tip
                        and chain["blocks"] == original_chain["blocks"],
                        "original chain tip was not restored; inspect node before further work")
                    require(node.call("getblockhash", [height]) == block,
                            "reconsidered transaction block is absent from the original chain")
                    write_json(evidence / "03-restored-node.json", restored_chain)
                    restored_payment = service_refresh(harness)
                    require(restored_payment["txid"] == txid
                            and restored_payment["chain_observation"].get("state") == "confirmed"
                            and restored_payment["chain_observation"].get("blockhash") == block,
                            "service did not restore confirmed observation of the original transaction")
                    write_json(evidence / "03-restored-service.json", restored_payment)
                    require(reference_refresh(harness, args.ledger, evidence, "03-restored").get("paid") is True,
                            "reference app did not recover paid after the original chain was restored")
                    restored = True
            after = {label: stable_database_state(path) for label, path in databases.items()}
            write_json(evidence / "state-after.json", after)
            require(after == before, "durable construction/submission/ownership state changed")
            require(ledger_bindings(args.ledger) == bindings, "original business bindings changed")
            require(not entries(observed.wallet_journal), "observation unexpectedly called the wallet")
            require(not observed.submissions(), "observation unexpectedly submitted transaction bytes")
            require(all(value == 0 for value in observed.calls().values()),
                    "observation unexpectedly called a construction stage")
            require(artifact_hashes(config) == tested, "tested executables/locks/orchestrator changed")
            require(control_attempts == {"invalidateblock": 1, "reconsiderblock": 1},
                    "controlled reorg must use exactly one attempt of each node mutation")
            summary = {"passed": True, "real_node_and_service": True, "payment_id": PAYMENT,
                "txid": txid, "blockhash": block, "original_chain_restored": restored,
                "control_rpc_attempts": control_attempts, "reference_paid_transition": [True, False, True],
                "invalidated_lookup_state": invalid_observation["state"],
                "invalidated_mining_reversed": invalid_observation["mining_reversed"],
                "controlled_node_invalidation_receipt_present": True,
                "unknown_lookup_is_proof_of_reversal": False,
                "construction_rpc_calls": 0, "wallet_rpc_calls": 0, "transaction_submission_calls": 0,
                "durable_owners_bytes_artifacts_attempts_unchanged": True,
                "business_bindings_unchanged": True, "tested_artifacts": tested}
            write_json(evidence / "summary.json", summary)
            print(json.dumps(summary, indent=2), flush=True)
    except BaseException as error:
        write_json(evidence / "aborted.json", {"passed": False, "error_type": type(error).__name__,
            "error": str(error), "control_rpc_attempts": control_attempts,
            "original_chain_restored_and_reconfirmed": restored,
            "instruction": "Do not replay uncertain node RPCs or any wallet operation; inspect receipts and chain state."})
        raise
    finally:
        harness.stop()
        for log in harness.logs:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--primary-db", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Perform the reviewed one-shot controlled node reorg")
    args = parser.parse_args()
    config = load_config(args.config)
    args.primary_db, args.ledger = args.primary_db.resolve(), args.ledger.resolve()
    suite_work = Path(config["work_dir"])
    require(args.primary_db == suite_work / "primary.db",
            "use the original completed-run primary DB; never a copy or replacement DB")
    require(args.ledger == suite_work / "successes" / "business.db",
            "use the exact successes/business.db business ledger from that same run")
    require(args.work_dir.resolve() != suite_work
            and args.evidence_dir.resolve() != Path(config["evidence_dir"]),
            "reorg qualification needs separate fresh work and evidence roots")
    plan = {"execute": args.execute, "payment_id": PAYMENT,
        "primary_db": str(args.primary_db), "business_ledger": str(args.ledger),
        "work_dir": str(args.work_dir.resolve()), "evidence_dir": str(args.evidence_dir.resolve()),
        "node_profile": "fixed regtest genesis and exact NU6.3 activation schedule",
        "requires_successful_nine_scenario_summary": True,
        "sequence": ["confirm existing payment and paid binding", "invalidate its real mined block once",
                     "observe confirmation loss and paid false", "finally reconsider same block once",
                     "verify original tip, confirmed observation and paid true"],
        "automatic_replay": False, "wallet_rpc_calls": False, "construction_or_submission_calls": False,
        "sql_updates_to_outbox": False, "release_or_replace_quarantined_owners": False,
        "unknown_lookup_proves_mining_reversal": False}
    print(json.dumps(plan, indent=2), flush=True)
    if args.execute:
        execute(args, config, plan)


if __name__ == "__main__":
    main()
