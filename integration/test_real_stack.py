"""One-shot real-stack scenarios. Default is configuration/plan validation only.

--execute uses the compiled Rust service, genuine Zallet/Zebra RPCs, and forwarding
proxies. It never edits a wallet DB, releases a construction owner, reuses a
quarantined wallet with another outbox DB, or replays an uncertain wallet RPC.
The final two scenarios permanently quarantine two distinct disposable wallets.
Only this program's Rust app process and proxy servers are managed here.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from fault_proxy import FaultServer
from qualification_artifacts import artifact_hashes as qualification_artifact_hashes
from qualify_pczt import NoRedirect, Rpc, local_origin, qualified_chain

CONSTRUCTION = ("pczt_create", "pczt_prove", "pczt_sign", "pczt_extract")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def entries(path):
    if not path.exists():
        return []
    result = []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            # Another thread may be appending only the final incomplete line.
            require(index == len(lines) - 1 and not line.endswith("\n"),
                    "fault proxy journal contains an invalid complete record")
    return result


def wait_until(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise TimeoutError(message)


def db_state(path, payment):
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT id,stage,inflight,owner,raw,byte_digest,txid,last_error FROM payments WHERE id=?", (payment,)).fetchone()
        require(row is not None, "missing durable payment record")
        result = dict(row)
        result["wallet_owner"] = db.execute("SELECT payment FROM wallet WHERE id=1").fetchone()[0]
        result["attempts"] = [dict(r) for r in db.execute("SELECT byte_digest,outcome FROM attempts WHERE payment=? ORDER BY id", (payment,))]
        return result


def public_state(state):
    result = {key: value for key, value in state.items() if key != "raw"}
    result["raw_present"] = state["raw"] is not None
    result["raw_sha256"] = None if state["raw"] is None else sha(state["raw"])
    return result


class Api:
    def __init__(self, origin, token):
        self.origin, self.token = local_origin(origin), token

    def call(self, method, path, body=None, timeout=900):
        request = urllib.request.Request(self.origin + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with client.open(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
        except (OSError, urllib.error.URLError) as error:
            return None, {"transport_error": type(error).__name__}


class ProgressFaultServer(FaultServer):
    def record(self, event, **details):
        super().record(event, **details)
        if event == "forward_started" and details.get("method") in (*CONSTRUCTION, "sendrawtransaction"):
            print(json.dumps({"progress": "real_rpc_forwarded", "stage": details["method"]}), flush=True)


class Harness:
    def __init__(self, config, work, evidence, node):
        self.config, self.work, self.evidence, self.node = config, work, evidence, node
        self.records = []
        self.process = None
        self.current = None
        self.token = secrets.token_urlsafe(48)
        self.token_file = work / "api-token.txt"
        descriptor = os.open(self.token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(self.token)
        self.environment = dict(os.environ, RAYON_NUM_THREADS="2")
        self.logs = []

    def record(self, name, **facts):
        result = {"scenario": name, "passed": True, "real_wallet_and_node": True, **facts}
        write_json(self.evidence / (name + ".json"), result)
        self.records.append(result)
        print(json.dumps({"scenario": name, "passed": True}), flush=True)

    def start(self, group, database=None):
        require(self.process is None, "only one owned service process may run")
        database = group.database if database is None else database
        index = group.launches
        group.launches += 1
        config_path = group.runtime / f"live-{index}.json"
        write_json(config_path, {
            "wallet_url": group.wallet_url, "node_url": group.node_url,
            "credential_file": group.wallet["credential_file"],
            "trusted_wallet": group.wallet["trusted_wallet"],
            "verifier": self.config["verifier"],
            "evidence_dir": str((group.runtime / f"adapter-{index}").resolve()),
        })
        command = [self.config["service"], "--db", str(database.resolve()),
            "--wallet", group.trusted["wallet"], "--account", group.trusted["account"],
            "--zebra-url", group.node_url, "--live-config", str(config_path.resolve()),
            "serve", "--listen", "127.0.0.1:0", "--token-file", str(self.token_file.resolve())]
        log_path = group.runtime / f"service-{index}.log"
        log = log_path.open("xb", buffering=0)
        self.logs.append(log)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
            env=self.environment, creationflags=flags)
        self.current = group

        def bound_address():
            if self.process.poll() is not None:
                raise RuntimeError("owned service exited before binding; inspect private service log")
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and "listen" in event:
                    address = event["listen"]
                    require(isinstance(address, str) and address.startswith("127.0.0.1:"),
                            "owned service reported an unexpected bind address")
                    port = address.removeprefix("127.0.0.1:")
                    require(port.isdecimal() and 0 < int(port) <= 65535,
                            "owned service did not report an assigned TCP port")
                    require(event.get("publication_enabled") is True,
                            "owned service startup has not enabled the qualified profile")
                    return "http://" + address
            return None

        origin = wait_until(bound_address, 60, "owned service did not report its bound address")
        self.api = Api(origin, self.token)

        def healthy():
            if self.process.poll() is not None:
                raise RuntimeError("owned service exited during startup; inspect private service log")
            status, health = self.api.call("GET", "/health", timeout=2)
            if status == 200:
                require(health.get("profile_qualified") is True
                        and health.get("publication_enabled") is True
                        and health.get("wallet_availability") == "not_probed",
                        "service executable has not enabled the qualified disposable profile")
                return health
            return None

        wait_until(healthy, 60, "owned live service failed readiness")

    def stop(self, crash=False):
        if self.process is not None:
            process, self.process = self.process, None
            if process.poll() is None:
                process.kill() if crash else process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
            return process.returncode

    def intent(self, wallet, amount=100000):
        chain, _ = qualified_chain(self.node)
        return {"network": "regtest", "wallet": wallet["wallet"], "account": wallet["account"],
            "recipient": {"address": self.config["secondary_registration"], "amount_zat": amount,
                          "memo_hex": "7265616c2d6f7574626f782d64656d6f"},
            "fee": {"rule": "zip317", "max_fee_zat": 50000},
            "expiry": {"min_height": chain["blocks"] + 1, "max_height": chain["blocks"] + 100},
            "privacy": "FullPrivacy"}

    def register(self, payment, intent):
        status, record = self.api.call("PUT", "/payments/" + payment, intent)
        require(status == 202, f"registration failed for {payment}: HTTP {status}")
        return record

    def retry(self, payment):
        status, record = self.api.call("POST", "/payments/" + payment + "/retry")
        require(status == 200 and record.get("stage") == "accepted", f"payment retry failed for {payment}: HTTP {status}")
        return record

    def observe(self, payment, intent, group):
        state = db_state(group.database, payment)
        require(state["raw"] is not None and sha(state["raw"]) == state["byte_digest"], "durable real bytes lack matching integrity digest")
        raw = bytes.fromhex(self.node.call("getrawtransaction", [state["txid"], 0]))
        require(raw == state["raw"], "node did not retain the exact committed bytes")
        pool = self.node.call("getrawmempool", [])
        require(state["txid"] in pool, "accepted transaction absent from real mempool before mining")
        # A single authorized mining RPC, never retried after uncertain outcome.
        self.node.call("generate", [1])
        receiver = Rpc(self.config["secondary"]["wallet_url"],
            Path(self.config["secondary"]["credential_file"]).read_text(), 30)
        approved = bytes.fromhex(intent["recipient"]["memo_hex"])
        memo = (approved + bytes(512 - len(approved))).hex() if approved else (b"\xf6" + bytes(511)).hex()

        def received():
            try:
                view = receiver.call("z_viewtransaction", [state["txid"]])
            except (RuntimeError, OSError, urllib.error.URLError):
                return None
            if view.get("status") != "mined" or type(view.get("confirmations")) is not int or view["confirmations"] < 1:
                return None
            matches = [output for output in view.get("outputs", [])
                if output.get("pool") == "ironwood" and output.get("outgoing") is False
                and output.get("account_uuid") == self.config["secondary_account"]
                and output.get("valueZat") == intent["recipient"]["amount_zat"]
                and output.get("memo") == memo and output.get("walletInternal") is False]
            require(len(matches) <= 1, "receiver observes duplicate approved payment outputs")
            return view if len(matches) == 1 else None

        view = wait_until(received, 120, "receiver wallet did not observe exact payment/memo")
        status, refreshed = self.api.call("POST", "/payments/" + payment + "/refresh", {"confirmations": 1})
        require(status == 200 and refreshed.get("chain_observation", {}).get("state") == "confirmed", "service did not record real confirmation observation")
        return {"txid": state["txid"], "raw_sha256": sha(raw), "receiver_verified": True,
                "memo_sha256": sha(bytes.fromhex(memo)), "receiver_status": view["status"],
                "receiver_confirmations": view["confirmations"],
                "chain_observation": refreshed.get("chain_observation")}


class Group:
    def __init__(self, harness, name, wallet, database, node_drop=False, wallet_drop=False):
        self.harness, self.wallet, self.database = harness, wallet, database
        self.runtime = harness.work / name
        self.runtime.mkdir()
        self.trusted = json.loads(Path(wallet["trusted_wallet"]).read_text())
        self.launches = 0
        self.node_journal, self.wallet_journal = (harness.evidence / (name + suffix) for suffix in ("-node.jsonl", "-wallet.jsonl"))
        self.node_release, self.wallet_release = self.runtime / "release-node", self.runtime / "release-wallet"
        self.node_proxy = ProgressFaultServer(("127.0.0.1", 0), harness.config["node_url"], self.node_journal,
            "sendrawtransaction", int(node_drop), 900, self.node_release if node_drop else None, 60)
        self.wallet_proxy = ProgressFaultServer(("127.0.0.1", 0), wallet["wallet_url"], self.wallet_journal,
            "pczt_extract", int(wallet_drop), 900, self.wallet_release if wallet_drop else None, 60)
        self.node_url = f"http://127.0.0.1:{self.node_proxy.server_port}"
        self.wallet_url = f"http://127.0.0.1:{self.wallet_proxy.server_port}"
        self.threads = [threading.Thread(target=proxy.serve_forever) for proxy in (self.node_proxy, self.wallet_proxy)]
        for thread in self.threads:
            thread.start()

    def calls(self, methods=CONSTRUCTION):
        return {method: sum(e.get("event") == "forward_started" and e.get("method") == method
                           for e in entries(self.wallet_journal)) for method in methods}

    def submissions(self):
        return [e for e in entries(self.node_journal) if e.get("event") == "forward_started" and e.get("method") == "sendrawtransaction"]

    def close(self):
        self.harness.stop()
        for path in (self.node_release, self.wallet_release):
            if not path.exists():
                path.touch()
        for proxy in (self.node_proxy, self.wallet_proxy):
            proxy.shutdown()
            # Already-forwarded operations finish; caller death does not cancel them.
            proxy.server_close()
        for thread in self.threads:
            thread.join()


@contextmanager
def group(harness, *args, **kwargs):
    value = Group(harness, *args, **kwargs)
    try:
        yield value
    finally:
        value.close()


def run_successes(h, primary_db):
    primary = h.config["primary"]
    with group(h, "successes", primary, primary_db) as g:
        h.start(g)
        approved = h.intent(g.trusted)
        h.register("ordinary-001", approved)
        record = h.retry("ordinary-001")
        counts = g.calls()
        require(all(v == 1 for v in counts.values()), "ordinary path did not execute each real wallet stage once")
        again = h.register("ordinary-001", approved)
        h.retry("ordinary-001")
        require(again["txid"] == record["txid"] and g.calls() == counts, "completed repeat constructed another transaction")
        observed = h.observe("ordinary-001", approved, g)
        h.record("01-ordinary-and-repeat", wallet_construction_calls=counts, **observed)

        for label, mutate in {
            "recipient": lambda v: v["recipient"].update(address=g.trusted["registration_address"]),
            "amount": lambda v: v["recipient"].update(amount_zat=100001),
            "memo": lambda v: v["recipient"].update(memo_hex="00"),
            "fee-policy": lambda v: v["fee"].update(max_fee_zat=50001),
            "expiry-policy": lambda v: v["expiry"].update(max_height=v["expiry"]["max_height"] + 1),
        }.items():
            changed = copy.deepcopy(approved)
            mutate(changed)
            status, _ = h.api.call("PUT", "/payments/ordinary-001", changed)
            require(status == 409, "changed existing-ID intent was not rejected as a conflict: " + label)
        require(g.calls() == counts, "conflicting intent caused a real wallet construction")
        h.record("04-changed-intent-rejected", conflicts=5, no_additional_construction=True)

        approved = h.intent(g.trusted)
        h.register("same-id-concurrent", approved)
        before = g.calls()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: h.api.call("POST", "/payments/same-id-concurrent/retry"), range(4)))
        require(any(status == 200 for status, _ in results) and all(status in (200, 409) for status, _ in results), "concurrent same-ID outcomes invalid")
        status, record = h.api.call("GET", "/payments/same-id-concurrent")
        require(status == 200 and record["stage"] == "accepted", "same-ID winner did not complete")
        require(all(g.calls()[method] - before[method] == 1 for method in CONSTRUCTION), "concurrent retries constructed more than one pipeline")
        observed = h.observe("same-id-concurrent", approved, g)
        h.record("03-concurrent-same-id", statuses=[r[0] for r in results], one_real_pipeline=True, **observed)

        # Use the actual reference application to register the bounty/refund IDs.
        approved = h.intent(g.trusted)
        intent_file = g.runtime / "reference-intent.json"
        write_json(intent_file, approved)
        reference = Path(__file__).resolve().parent.parent / "examples" / "bounty_refund.py"
        payments = ["bounty:real-stack-award", "refund:real-stack-purchase"]
        ledger = g.runtime / "business.db"
        for kind, business_id in (("bounty", "real-stack-award"), ("refund", "real-stack-purchase")):
            command = [sys.executable, str(reference), "--url", h.api.origin, "--token-file", str(h.token_file),
                "--ledger", str(ledger), "register", "--kind", kind, "--business-id", business_id,
                "--intent", str(intent_file), "--confirmations", "1"]
            completed = subprocess.run(command, capture_output=True, timeout=30)
            require(completed.returncode == 0, "reference app registration failed")
        before = g.calls()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda payment: h.api.call("POST", "/payments/" + payment + "/retry"), payments))
        require(all(status in (200, 409) for status, _ in results), "two-ID contention produced an unexpected outcome")
        for payment in payments:
            status, state = h.api.call("GET", "/payments/" + payment)
            require(status == 200, "two-ID payment missing")
            if state["stage"] != "accepted":
                require(state["inflight"] is None and state["owner"] is None and state["stage"] == "pending", "cannot retry an uncertain owner")
                h.retry(payment)
        require(all(g.calls()[method] - before[method] == 2 for method in CONSTRUCTION), "two IDs did not construct exactly two pipelines")
        with sqlite3.connect(primary_db) as db:
            sequence = [row[0] for row in db.execute("SELECT payment FROM events WHERE kind='stage_started' AND payment IN (?,?) ORDER BY id", payments)]
        require(sequence in ([payments[0]] * 4 + [payments[1]] * 4, [payments[1]] * 4 + [payments[0]] * 4), "construction stages interleaved across dedicated wallet")
        # Both are accepted now; mine them together, checking both exact transactions.
        observations = []
        for payment in payments:
            state = db_state(primary_db, payment)
            observed = bytes.fromhex(h.node.call("getrawtransaction", [state["txid"], 0]))
            require(observed == state["raw"], "two-ID node bytes differ from committed bytes")
        # observe() requires mempool membership, so settle each sequentially by
        # checking the second via read-only receiver lookup after the first mine.
        observations.append(h.observe(payments[0], approved, g))
        observations.append(observe_already_mined(h, payments[1], approved, g))
        # New reference-app processes reopen the same durable business ledger
        # after a real service restart; their pay operation is an idempotent retry.
        constructions = g.calls()
        h.stop()
        h.start(g)
        business_results = []
        for kind, business_id in (("bounty", "real-stack-award"), ("refund", "real-stack-purchase")):
            for operation in ("query", "pay", "refresh"):
                command = [sys.executable, str(reference), "--url", h.api.origin,
                    "--token-file", str(h.token_file), "--ledger", str(ledger), operation,
                    "--kind", kind, "--business-id", business_id]
                completed = subprocess.run(command, capture_output=True, timeout=120)
                require(completed.returncode == 0, "reference app restart/query/pay/refresh failed")
                result = json.loads(completed.stdout)
                require(result.get("paid") is True and result.get("required_confirmations") == 1,
                        "reference app did not retain confirmed paid state after restart")
                business_results.append({"payment_id": result["payment_id"], "operation": operation,
                    "paid": result["paid"], "txid": result["payment"]["txid"]})
        require(g.calls() == constructions, "reference app idempotent pay constructed another transaction")
        h.record("05-two-ids-and-reference-app", stages_serialized=True, observations=observations,
                 durable_business_ledger=True, actual_service_restart=True, business_results=business_results)


def observe_already_mined(h, payment, intent, g):
    state = db_state(g.database, payment)
    raw = bytes.fromhex(h.node.call("getrawtransaction", [state["txid"], 0]))
    require(raw == state["raw"], "mined node bytes differ from committed bytes")
    receiver = Rpc(h.config["secondary"]["wallet_url"], Path(h.config["secondary"]["credential_file"]).read_text(), 30)
    memo = bytes.fromhex(intent["recipient"]["memo_hex"]).ljust(512, b"\0").hex()
    def received():
        try:
            view = receiver.call("z_viewtransaction", [state["txid"]])
        except (RuntimeError, OSError, urllib.error.URLError):
            return False
        if view.get("status") != "mined" or type(view.get("confirmations")) is not int or view["confirmations"] < 1:
            return False
        return view if any(o.get("pool") == "ironwood" and o.get("outgoing") is False
                   and o.get("account_uuid") == h.config["secondary_account"]
                   and o.get("valueZat") == intent["recipient"]["amount_zat"] and o.get("memo") == memo
                   and o.get("walletInternal") is False
                   for o in view.get("outputs", [])) else False
    view = wait_until(received, 120, "receiver did not observe second mined payout")
    status, refreshed = h.api.call("POST", "/payments/" + payment + "/refresh", {"confirmations": 1})
    require(status == 200 and refreshed.get("chain_observation", {}).get("state") == "confirmed", "second payout not independently confirmed")
    return {"txid": state["txid"], "raw_sha256": sha(raw), "receiver_verified": True,
            "receiver_status": view["status"], "receiver_confirmations": view["confirmations"]}


def run_node_loss(h, primary_db):
    with group(h, "node-loss", h.config["primary"], primary_db, node_drop=True) as g:
        h.start(g)
        approved = h.intent(g.trusted)
        payment = "node-acceptance-response-lost"
        h.register(payment, approved)
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(h.api.call, "POST", "/payments/" + payment + "/retry")
            wait_until(lambda: any(e["event"] == "response_release_wait" for e in entries(g.node_journal)), 900, "node response hold not reached")
            responses = [e for e in entries(g.node_journal) if e["event"] == "upstream_response_complete" and e.get("method") == "sendrawtransaction"]
            require(len(responses) == 1 and responses[0]["rpc_error"] is False, "actual node did not accept held submission")
            before = db_state(primary_db, payment)
            require(before["raw"] is not None and before["stage"] == "committed" and len(before["attempts"]) == 1, "missing durable preacceptance state")
            require(before["txid"] in h.node.call("getrawmempool", []), "acceptance not independently observed in real mempool")
            constructions = g.calls()
            h.stop(crash=True)
            g.node_release.touch(exist_ok=False)
            request.result(timeout=20)
        wait_until(lambda: any(e["event"] == "response_dropped" for e in entries(g.node_journal)), 20, "held response was not dropped")
        h.start(g)
        h.retry(payment)
        after = db_state(primary_db, payment)
        require(after["raw"] == before["raw"] and after["byte_digest"] == before["byte_digest"] and after["txid"] == before["txid"], "restart replaced committed bytes")
        require(g.calls() == constructions, "postcommit retry called a wallet construction stage")
        submissions = g.submissions()
        require(len(submissions) == 2 and all(s.get("transaction_sha256") == before["byte_digest"] for s in submissions), "proxy did not observe two identical real transaction byte submissions")
        require(all(attempt["byte_digest"] == before["byte_digest"] for attempt in after["attempts"]), "attempts refer to differing byte digests")
        h.record("02-lost-node-response-restart", before=public_state(before), after=public_state(after),
                 identical_wire_sha256=[s["transaction_sha256"] for s in submissions], no_additional_construction=True,
                 **h.observe(payment, approved, g))


def run_integrity(h, primary_db):
    corrupted = h.work / "integrity-copy.db"
    with sqlite3.connect(f"file:{primary_db.resolve().as_posix()}?mode=ro", uri=True) as source, sqlite3.connect(corrupted) as destination:
        source.backup(destination)
    payment = "ordinary-001"
    original = db_state(corrupted, payment)
    data = bytearray(original["raw"])
    data[-1] ^= 1
    with sqlite3.connect(corrupted) as db:
        # Hardware/storage-corruption simulation against an OFFLINE COPY ONLY.
        db.execute("DROP TRIGGER final_immutable")
        db.execute("UPDATE payments SET raw=? WHERE id=?", (data, payment))
    with group(h, "integrity-copy", h.config["primary"], corrupted) as g:
        h.start(g)
        get_status, _ = h.api.call("GET", "/payments/" + payment)
        retry_status, _ = h.api.call("POST", "/payments/" + payment + "/retry")
        require(get_status >= 400 and retry_status >= 400, "stored-byte corruption was not rejected on read/retry")
        require(not g.submissions() and all(v == 0 for v in g.calls().values()), "corrupt copy triggered publication or construction")
        h.record("09-integrity-copy-rejected", original_database_modified=False,
                 get_status=get_status, retry_status=retry_status, no_submission=True, no_construction=True)


def run_terminal_wallet_loss(h, primary_db):
    with group(h, "terminal-wallet-loss", h.config["primary"], primary_db, wallet_drop=True) as g:
        h.start(g)
        approved = h.intent(g.trusted)
        payment = "terminal-extraction-response-loss"
        h.register(payment, approved)
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(h.api.call, "POST", "/payments/" + payment + "/retry")
            wait_until(lambda: any(e["event"] == "forward_started" and e.get("method") == "pczt_extract" for e in entries(g.wallet_journal)), 900, "real extraction not forwarded")
            completed_before_death = any(e["event"] == "upstream_response_complete" and e.get("method") == "pczt_extract" for e in entries(g.wallet_journal))
            h.stop(crash=True)
            wait_until(lambda: any(e["event"] == "response_release_wait" and e.get("method") == "pczt_extract" for e in entries(g.wallet_journal)), 900, "remote extraction did not complete after app death")
            responses = [e for e in entries(g.wallet_journal) if e["event"] == "upstream_response_complete" and e.get("method") == "pczt_extract"]
            require(len(responses) == 1 and responses[0]["rpc_error"] is False, "remote extraction did not succeed")
            g.wallet_release.touch(exist_ok=False)
            request.result(timeout=20)
        before = db_state(primary_db, payment)
        require(before["inflight"] == "extract" and before["owner"] and before["raw"] is None and before["wallet_owner"] == payment,
                "uncertain extraction failed to retain wallet ownership")
        calls = g.calls()
        h.start(g)
        retry_status, _ = h.api.call("POST", "/payments/" + payment + "/retry")
        require(retry_status == 409, "restart replayed an uncertain extraction")
        h.register("blocked-by-quarantine", h.intent(g.trusted))
        blocked_status, _ = h.api.call("POST", "/payments/blocked-by-quarantine/retry")
        require(blocked_status == 409, "another ID stole quarantined wallet")
        after = db_state(primary_db, payment)
        require(after["owner"] == before["owner"] and after["inflight"] == "extract" and after["wallet_owner"] == payment,
                "restart changed construction ownership")
        require(g.calls() == calls and not g.submissions(), "uncertain-wallet restart caused construction/submission")
        h.record("07-wallet-response-loss-quarantine", before=public_state(before), after=public_state(after),
                 caller_killed_before_remote_completion=not completed_before_death,
                 caller_killed_before_held_response_delivery=True, remote_operation_completed=True,
                 retry_status=retry_status, another_id_status=blocked_status, no_submission=True,
                 wallet_quarantined=True, no_more_construction_on_primary=True)
        restart = h.config.get("wallet_restart_evidence")
        h.record("08-app-restart-and-remote-completion", actual_app_restart=True,
                 durable_owner_retained=True, remote_completion_after_app_death=not completed_before_death,
                 prior_wallet_restart_evidence_sha256=None if not restart else sha(Path(restart).read_bytes()),
                 prior_wallet_restart_evidence_is_external=bool(restart))


def run_terminal_commit_failure(h):
    database = h.work / "secondary-commit-failure.db"
    with group(h, "terminal-commit-failure", h.config["secondary"], database) as g:
        h.start(g)
        approved = h.intent(g.trusted, amount=10000)
        approved["recipient"]["address"] = h.config["primary_registration"]
        payment = "terminal-sqlite-commit-rejection"
        h.register(payment, approved)
        h.stop()
        with sqlite3.connect(database) as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE qualification_commit_parent(id INTEGER PRIMARY KEY);
                CREATE TABLE qualification_commit_child(parent INTEGER REFERENCES qualification_commit_parent(id) DEFERRABLE INITIALLY DEFERRED);
                CREATE TRIGGER qualification_reject_final_commit AFTER UPDATE OF raw ON payments
                WHEN OLD.raw IS NULL AND NEW.raw IS NOT NULL
                BEGIN INSERT INTO qualification_commit_child(parent) VALUES(1); END;
            """)
        h.start(g)
        status, error = h.api.call("POST", "/payments/" + payment + "/retry")
        state = db_state(database, payment)
        require(status == 500 and "FOREIGN KEY" in error.get("error", "").upper(), "actual deferred SQLite COMMIT rejection not observed")
        require(state["raw"] is None and state["txid"] is None and state["inflight"] == "extract" and state["owner"]
                and state["wallet_owner"] == payment and not state["attempts"], "rejected final COMMIT persisted bytes/released owner/attempted submission")
        require(not g.submissions() and all(g.calls()[m] == 1 for m in CONSTRUCTION), "COMMIT fault did not exercise exactly one genuine pipeline with zero submission")
        with sqlite3.connect(database) as db:
            child_rows = db.execute("SELECT count(*) FROM qualification_commit_child").fetchone()[0]
        require(child_rows == 0, "failed final COMMIT left partial transaction writes")
        h.record("06-real-sqlite-commit-rejection", http_status=status, sqlite_error=error["error"],
                 state=public_state(state), deferred_foreign_key_child_rows=child_rows,
                 actual_wallet_pipeline=True, zero_outbox_submission=True, separate_wallet_quarantined=True)


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    require(type(config.get("include_bound_create_recovery", False)) is bool,
            "include_bound_create_recovery must be a JSON boolean")
    if config.get("include_bound_create_recovery", False):
        require(Path(__file__).with_name("test_bound_create_recovery.py").is_file(),
                "bound CREATE qualification module must be present before the run")
    for key in ("service", "verifier", "work_dir", "evidence_dir"):
        config[key] = str(Path(config[key]).resolve())
    require(Path(config["service"]).is_file() and Path(config["verifier"]).is_file(), "compiled service/helper required")
    local_origin(config["node_url"])
    for label in ("primary", "secondary"):
        wallet = config[label]
        local_origin(wallet["wallet_url"])
        for key in ("credential_file", "trusted_wallet"):
            wallet[key] = str(Path(wallet[key]).resolve())
            require(Path(wallet[key]).is_file(), "missing private credential/bootstrap input")
        trusted = json.loads(Path(wallet["trusted_wallet"]).read_text())
        config[label + "_registration"] = trusted["registration_address"]
        config[label + "_account"] = trusted["account"]
    require(config["primary_account"] != config["secondary_account"]
            and config["primary"]["wallet_url"] != config["secondary"]["wallet_url"], "terminal faults require distinct actual wallets/accounts")
    if config.get("wallet_restart_evidence"):
        require(Path(config["wallet_restart_evidence"]).is_file(), "prior wallet restart evidence path missing")
    return config


def inspect_existing(config):
    """Read-only recovery aid. Never starts an app/proxy or replays a request."""
    result = {"read_only": True, "automatic_resume": False, "databases": {}}
    work = Path(config["work_dir"])
    for name in ("primary.db", "secondary-commit-failure.db", "integrity-copy.db"):
        path = work / name
        if not path.is_file():
            continue
        with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            records = [dict(row) for row in db.execute(
                "SELECT id,stage,inflight,owner,byte_digest,txid,last_error FROM payments ORDER BY id")]
            owner = db.execute("SELECT payment FROM wallet WHERE id=1").fetchone()[0]
        result["databases"][name] = {"wallet_owner": owner, "payments": records}
    node = Rpc(config["node_url"], None, 30)
    try:
        result["node_height"] = node.call("getblockchaininfo", [])["blocks"]
        result["mempool_txids"] = node.call("getrawmempool", [])
    except (RuntimeError, OSError, urllib.error.URLError) as error:
        result["node_read_error"] = type(error).__name__
    print(json.dumps(result, indent=2), flush=True)


def artifact_hashes(config):
    return qualification_artifact_hashes(config, orchestrator=Path(__file__).resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--execute", action="store_true", help="Actually run reviewed disposable-wallet scenarios; terminal faults retain quarantine")
    action.add_argument("--inspect-existing", action="store_true", help="Read-only durable states/node observations after an aborted run; never resumes mutations")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.inspect_existing:
        inspect_existing(config)
        return
    plan = {"execute": args.execute, "uses_real_wallets_and_node": True,
            "include_bound_create_recovery": config.get("include_bound_create_recovery", False),
            "order": ["ordinary/repeat", "changed-intent", "same-ID concurrency", "bounty/refund two-ID serialization",
                      "lost node acceptance/app restart", "offline integrity copy", "terminal primary extraction loss",
                      "terminal separate secondary COMMIT rejection"],
            "wallet_database_edits": False, "automatic_wallet_operation_replay": False,
            "wallet_owner_release_or_lease_stealing": False,
            "terminal_faults_quarantine_distinct_wallets": True,
            "wallet_process_restart_exercised_by_runner": False,
            "prior_external_wallet_restart_evidence_supplied": bool(config.get("wallet_restart_evidence"))}
    if plan["include_bound_create_recovery"]:
        plan["order"].insert(0, "bound same-owner CREATE recovery and cross-ID evidence rejection")
    print(json.dumps(plan, indent=2), flush=True)
    if not args.execute:
        return
    work, evidence = Path(config["work_dir"]), Path(config["evidence_dir"])
    require(not work.exists() and not evidence.exists(), "one-shot work/evidence directories must be fresh; no automatic resume")
    node = Rpc(config["node_url"], None, 30)
    chain, _ = qualified_chain(node)
    work.mkdir(parents=True, mode=0o700)
    evidence.mkdir(parents=True)
    write_json(evidence / "reviewed-plan.json", plan)
    write_json(evidence / "initial-node.json", {"blocks": chain["blocks"], "consensus": chain["consensus"], "upgrades": chain["upgrades"]})
    tested_artifacts = artifact_hashes(config)
    write_json(evidence / "tested-artifacts.json", tested_artifacts)
    h = Harness(config, work, evidence, node)
    bound_create_recovery = None
    try:
        primary_db = work / "primary.db"
        if config.get("include_bound_create_recovery", False):
            from test_bound_create_recovery import run_bound_create_recovery
            bound_create_recovery = run_bound_create_recovery(h, primary_db)
            require(isinstance(bound_create_recovery, dict)
                    and bound_create_recovery.get("passed") is True,
                    "bound CREATE recovery/rejection qualification did not pass")
            require(not h.records, "bound CREATE supplement must not replace any of the nine scenarios")
            require(artifact_hashes(config) == tested_artifacts,
                    "tested sources/harnesses/executables changed during bound CREATE qualification")
        run_successes(h, primary_db)
        run_node_loss(h, primary_db)
        run_integrity(h, primary_db)
        # No primary construction is permitted after this terminal scenario.
        run_terminal_wallet_loss(h, primary_db)
        # This is a distinct dedicated wallet/account/DB, never a primary-claim bypass.
        run_terminal_commit_failure(h)
        require(len(h.records) == 9, "all nine real scenario reports are required")
        require(artifact_hashes(config) == tested_artifacts,
                "tested sources/harnesses/executables changed during run")
        write_json(evidence / "summary.json", {"passed": True, "mock_wallet_or_node": False,
            "scenarios": h.records, "primary_and_secondary_quarantined": True,
            "bound_create_recovery": bound_create_recovery,
            "tested_artifacts": tested_artifacts,
            "wallet_process_restart_scope": {"performed_in_this_run": False,
                "prior_external_evidence_supplied": bool(config.get("wallet_restart_evidence"))},
            "private_wallet_material_in_evidence": False})
        print(json.dumps({"passed": True, "real_scenarios": len(h.records),
            "bound_create_recovery_passed": None if bound_create_recovery is None else True,
            "both_terminal_wallets_quarantined": True}), flush=True)
    except BaseException as error:
        write_json(evidence / "aborted.json", {"passed": False, "error_type": type(error).__name__,
            "error": str(error), "completed_scenarios": h.records,
            "bound_create_recovery": bound_create_recovery,
            "instruction": "Do not rerun/release owners/switch DBs; inspect durable states and reconcile."})
        raise
    finally:
        h.stop()
        for log in h.logs:
            log.close()


if __name__ == "__main__":
    main()
