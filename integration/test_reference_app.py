"""Local application/SQLite protocol tests; no wallet or chain success is modeled."""
from contextlib import closing
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest


PROJECT = Path(__file__).resolve().parents[1]
APP = PROJECT / "examples" / "bounty_refund.py"


class ReferenceAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="outbox-business-ledger-")
        self.root = Path(self.temporary.name)
        self.ledger = self.root / "business.db"
        self.token = self.root / "token"
        self.token.write_text("local-protocol-test-token-only", encoding="ascii")
        self.intent = json.loads((PROJECT / "examples" / "intent.json").read_text())
        self.intent_path = self.root / "intent.json"
        self.intent_path.write_text(json.dumps(self.intent), encoding="utf-8")
        self.payment = {"id": "refund:purchase-7", "intent": self.intent, "stage": "pending",
                        "txid": None, "chain_observation": None}
        self.calls = []
        self.bound_before_http = []
        self.drop_retry = False
        self.redirect_health = False
        self.hold_retry = False
        self.forwarded = threading.Event()
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.handle_request()

            def do_PUT(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.calls.append((self.command, self.path, json.loads(body) if body else None))
                with closing(sqlite3.connect(owner.ledger)) as connection:
                    row = connection.execute("SELECT intent_json FROM business_payments WHERE payment_id=?",
                                             ("refund:purchase-7",)).fetchone()
                    owner.bound_before_http.append(row is not None and json.loads(row[0]) == owner.intent)
                if self.path == "/health" and owner.redirect_health:
                    self.send_response(302)
                    self.send_header("Location", "/should-never-follow")
                    self.end_headers()
                    return
                if self.path.endswith("/retry"):
                    owner.forwarded.set()
                    if owner.hold_retry:
                        owner.release.wait(10)
                    if owner.drop_retry:
                        self.close_connection = True
                        return
                data = json.dumps({"state": "local-protocol-test"} if self.path == "/health" else owner.payment).encode()
                self.send_response(202 if self.command == "PUT" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                try:
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temporary.cleanup()

    def command(self, operation, *extra):
        command = [sys.executable, str(APP), "--url", "http://127.0.0.1:" + str(self.server.server_port),
                   "--token-file", str(self.token), "--ledger", str(self.ledger), operation,
                   "--kind", "refund", "--business-id", "purchase-7"]
        if operation == "register":
            command.extend(("--intent", str(self.intent_path)))
        return command + list(extra)

    def run_app(self, operation, *extra, expected=0, env=None):
        result = subprocess.run(self.command(operation, *extra), capture_output=True, text=True, timeout=15, env=env)
        self.assertEqual(expected, result.returncode, result.stderr + result.stdout)
        return result, json.loads(result.stdout) if result.stdout else None

    def state(self):
        with closing(sqlite3.connect(self.ledger)) as connection:
            connection.row_factory = sqlite3.Row
            return dict(connection.execute("SELECT * FROM business_payments").fetchone())

    def test_binding_precedes_http_and_survives_application_restart(self):
        _, registered = self.run_app("register")
        self.assertFalse(registered["paid"])
        self.assertTrue(all(self.bound_before_http))
        _, queried = self.run_app("query")  # A new process, with no intent argument.
        self.assertEqual(registered["payment"], queried["payment"])
        self.assertEqual(self.intent, json.loads(self.state()["intent_json"]))
        self.assertEqual(self.payment, json.loads(self.state()["outbox_state_json"]))

    def test_changed_intent_and_threshold_fail_before_http(self):
        self.run_app("register")
        count = len(self.calls)
        changed = deepcopy(self.intent)
        changed["recipient"]["amount_zat"] += 1
        self.intent_path.write_text(json.dumps(changed))
        self.run_app("register", expected=1)
        self.run_app("query", "--confirmations", "4", expected=1)
        self.assertEqual(count, len(self.calls))
        self.assertEqual(self.intent, json.loads(self.state()["intent_json"]))

    def test_identity_is_immutable_in_sqlite(self):
        self.run_app("register")
        with closing(sqlite3.connect(self.ledger)) as connection:
            for statement in ("UPDATE business_payments SET payment_id='new-id'",
                              "UPDATE business_payments SET intent_json='{}'",
                              "UPDATE business_payments SET intent_sha256='bad'",
                              "UPDATE business_payments SET required_confirmations=1",
                              "DELETE FROM business_payments"):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)
        self.assertEqual("refund:purchase-7", self.state()["payment_id"])

    def test_successful_wrong_intent_response_cannot_mark_paid_or_replace_state(self):
        self.run_app("register")
        original = self.state()["outbox_state_json"]
        self.payment = deepcopy(self.payment)
        self.payment["intent"]["recipient"]["amount_zat"] += 1
        self.payment.update(txid="test-id", stage="committed", chain_observation={
            "state": "confirmed", "confirmations": 3, "required_confirmations": 3, "txid": "test-id"})
        _, response = self.run_app("query", expected=1)
        self.assertFalse(response["paid"])
        self.assertEqual(original, self.state()["outbox_state_json"])

    def test_pay_only_retries_same_id_and_lost_response_keeps_binding_and_state(self):
        self.run_app("register")
        original = self.state()["outbox_state_json"]
        self.drop_retry = True
        _, response = self.run_app("pay", expected=1)
        self.assertFalse(response["paid"])
        self.assertEqual(original, self.state()["outbox_state_json"])
        retries = [call for call in self.calls if call[1].endswith("/retry")]
        self.assertEqual([("POST", "/payments/refund:purchase-7/retry", None)], retries)
        with closing(sqlite3.connect(self.ledger)) as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM business_payments").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT count(*) FROM attempts WHERE error IS NOT NULL").fetchone()[0])

    def test_paid_requires_confirmed_observation_and_bound_threshold(self):
        self.run_app("register")
        self.payment.update(txid="test-id", stage="committed")
        for state, count, expected in (("node_observed", 0, False), ("mined", 2, False),
                                       ("confirmed", 2, False), ("confirmed", 3, True), ("unknown", 0, False)):
            self.payment["chain_observation"] = {"state": state, "confirmations": count,
                                                 "required_confirmations": 3, "txid": "test-id"}
            _, response = self.run_app("refresh")
            self.assertIs(response["paid"], expected)
        refreshes = [call for call in self.calls if call[1].endswith("/refresh")]
        self.assertTrue(all(call == ("POST", "/payments/refund:purchase-7/refresh", {"confirmations": 3})
                            for call in refreshes))

    def test_caller_death_leaves_original_identity_and_unfinished_attempt(self):
        self.run_app("register")
        self.hold_retry = True
        process = subprocess.Popen(self.command("pay"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(self.forwarded.wait(5))
            process.kill()
            process.communicate(timeout=5)
            with closing(sqlite3.connect(self.ledger)) as connection:
                self.assertEqual(1, connection.execute("SELECT count(*) FROM business_payments").fetchone()[0])
                self.assertEqual(1, connection.execute("SELECT count(*) FROM attempts WHERE completed_at IS NULL").fetchone()[0])
            self.release.set()
            _, response = self.run_app("query")
            self.assertEqual("refund:purchase-7", response["payment_id"])
            self.assertFalse(response["paid"])
        finally:
            self.release.set()
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_proxies_are_ignored_and_redirects_are_not_followed(self):
        env = dict(os.environ, HTTP_PROXY="http://127.0.0.1:1", http_proxy="http://127.0.0.1:1",
                   ALL_PROXY="http://127.0.0.1:1", all_proxy="http://127.0.0.1:1", NO_PROXY="", no_proxy="")
        self.run_app("register", env=env)
        self.redirect_health = True
        _, response = self.run_app("query", expected=1)
        self.assertFalse(response["paid"])
        self.assertFalse(any(path == "/should-never-follow" for _, path, _ in self.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
