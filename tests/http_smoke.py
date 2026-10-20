#!/usr/bin/env python3
"""Exercise the real Rust CLI/API; no fake wallet or chain success is claimed."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=project / "target" / "debug" / "zcash-durable-outbox.exe")
    parser.add_argument("--json", type=Path, default=project / "evidence" / "http-smoke.json")
    args = parser.parse_args()
    binary = args.binary.resolve()
    if not binary.is_file():
        parser.error("compiled Rust binary does not exist: " + str(binary))
    checks = []
    node_requests = []

    class NodeTrap(BaseHTTPRequestHandler):
        def do_POST(self):
            node_requests.append(self.path)
            self.send_response(500)
            self.end_headers()
        def log_message(self, *ignored):
            pass

    trap = ThreadingHTTPServer(("127.0.0.1", 0), NodeTrap)
    trap_thread = threading.Thread(target=trap.serve_forever, daemon=True)
    trap_thread.start()
    service = None
    with tempfile.TemporaryDirectory(prefix="outbox-http-smoke-") as temporary:
        root = Path(temporary)
        db = root / "outbox.db"
        token_file = root / "token"
        token = secrets.token_urlsafe(36)
        token_file.write_text(token, encoding="ascii")
        intent_file = root / "intent.json"
        intent = json.loads((project / "examples" / "intent.json").read_text(encoding="utf-8"))
        intent_file.write_text(json.dumps(intent), encoding="utf-8")
        node_url = "http://127.0.0.1:" + str(trap.server_port)

        def cli(*command, expected=0):
            result = subprocess.run([str(binary), "--db", str(db), "--zebra-url", node_url, *command],
                                    text=True, capture_output=True, timeout=20)
            assert result.returncode == expected, (command, result.returncode, result.stderr)
            return json.loads(result.stdout if expected == 0 else result.stderr)

        def start():
            nonlocal service
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            base = "http://127.0.0.1:" + str(port)
            service = subprocess.Popen(
                [str(binary), "--db", str(db), "--zebra-url", node_url, "serve",
                 "--token-file", str(token_file), "--listen", "127.0.0.1:" + str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            deadline = time.monotonic() + 20
            while True:
                if service.poll() is not None:
                    raise AssertionError("service exited during startup: " + str(service.returncode)
                                         + "; " + service.stderr.read())
                try:
                    status, body = http(base, "GET", "/health")
                    assert status == 200
                    return base, body
                except (OSError, urllib.error.URLError):
                    assert time.monotonic() < deadline, "service startup timeout"
                    time.sleep(.05)

        def stop():
            nonlocal service
            if service is not None:
                service.terminate()
                service.communicate(timeout=10)
                service = None

        def http(base, method, path, body=None, authenticated=True):
            data = None if body is None else json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    assert response.headers["Cache-Control"] == "no-store"
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        try:
            initialized = cli("init")
            assert initialized["wallet_qualified"] is False
            assert initialized["publication_enabled"] is False
            registered = cli("submit", "cli-bounty", "--intent", str(intent_file))
            assert cli("submit", "cli-bounty", "--intent", str(intent_file)) == registered
            assert cli("get", "cli-bounty") == registered
            assert cli("unresolved") == [registered]
            assert "not qualified" in cli("retry", "cli-bounty", expected=1)["error"].lower()
            checks.append("actual Rust CLI init/submit/get/unresolved and blocked retry")

            base, health = start()
            assert health["wallet_qualified"] is False and health["publication_enabled"] is False
            checks.append("authenticated health reports publication disabled")
            for method, path, payload in (("GET", "/health", None), ("GET", "/payments", None),
                                          ("PUT", "/payments/no-auth", intent),
                                          ("POST", "/payments/cli-bounty/retry", None)):
                assert http(base, method, path, payload, authenticated=False)[0] == 401
            checks.append("read, registration, and retry require bearer authentication")

            status, original = http(base, "PUT", "/payments/bounty:task-7", intent)
            assert status == 202 and original["stage"] == "pending"
            reordered = dict(reversed(list(intent.items())))
            assert http(base, "PUT", "/payments/bounty:task-7", reordered) == (202, original)
            assert http(base, "GET", "/payments/bounty:task-7") == (200, original)
            checks.append("same business ID and canonical intent return existing payment")

            changes = [("recipient", "amount_zat", 100001), ("recipient", "memo_hex", "00"),
                       ("recipient", "address", "uregtest1anotherreceiver"),
                       ("fee", "max_fee_zat", 10001), ("expiry", "max_height", 501),
                       (None, "privacy", "AllowRevealedRecipients")]
            statuses = []
            for section, key, value in changes:
                changed = copy.deepcopy(intent)
                (changed if section is None else changed[section])[key] = value
                status, _ = http(base, "PUT", "/payments/bounty:task-7", changed)
                assert status in (400, 409), (section, key, status)
                statuses.append(status)
            assert http(base, "GET", "/payments/bounty:task-7")[1] == original
            checks.append("changed amount/memo/address/fee/expiry/privacy rejected: " + str(statuses))

            with ThreadPoolExecutor(max_workers=8) as pool:
                repeated = list(pool.map(lambda _: http(base, "PUT", "/payments/bounty:task-7", intent), range(12)))
                retries = list(pool.map(lambda _: http(base, "POST", "/payments/bounty:task-7/retry"), range(12)))
            assert all(item == (202, original) for item in repeated)
            assert all(status == 503 for status, _ in retries)
            checks.append("12 concurrent identical registrations and 12 blocked retries")

            for kind, business in (("bounty", "task-8"), ("refund", "purchase-2")):
                example = [sys.executable, str(project / "examples" / "bounty_refund.py"),
                           "--url", base, "--token-file", str(token_file)]
                registered_example = subprocess.run(example + ["register", "--kind", kind,
                    "--business-id", business, "--intent", str(intent_file)], text=True, capture_output=True, timeout=20)
                assert registered_example.returncode == 0, registered_example.stderr
                example_body = json.loads(registered_example.stdout)
                assert example_body["paid"] is False and example_body["payment"]["stage"] == "pending"
                queried = subprocess.run(example + ["query", "--kind", kind,
                    "--business-id", business], text=True, capture_output=True, timeout=20)
                assert queried.returncode == 0, queried.stderr
                assert json.loads(queried.stdout)["payment"] == example_body["payment"]
            checks.append("bounty and refund reference application registers and queries without claiming payment")

            listed = http(base, "GET", "/payments")[1]
            assert {item["id"] for item in listed} == {"cli-bounty", "bounty:task-7", "bounty:task-8", "refund:purchase-2"}
            stop()
            base, _ = start()
            assert http(base, "GET", "/payments/bounty:task-7") == (200, original)
            assert http(base, "GET", "/payments")[1] == listed
            assert http(base, "POST", "/payments/bounty:task-7/retry")[0] == 503
            checks.append("application restart preserves identities and pending intent records")
            stop()

            with closing(sqlite3.connect(db)) as connection:
                counts = {table: connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
                          for table in ("payments", "artifacts", "attempts", "events")}
                assert counts == {"payments": 4, "artifacts": 0, "attempts": 0, "events": 0}, counts
                assert connection.execute("SELECT count(*) FROM payments WHERE raw IS NOT NULL OR txid IS NOT NULL OR owner IS NOT NULL OR inflight IS NOT NULL").fetchone()[0] == 0
                assert connection.execute("SELECT payment FROM wallet WHERE id=1").fetchone()[0] is None
            assert node_requests == [], node_requests
            checks.append("SQLite records zero construction artifacts/events, transaction bytes or submission attempts; node trap received zero calls")

            # Synthetic restored state, deliberately NOT a Zcash transaction. This
            # proves the executable publication gate without claiming chain behavior.
            restored_raw = b"NOT A ZCASH TRANSACTION - RESTORE GATE FIXTURE"
            restored_digest = hashlib.sha256(restored_raw).hexdigest()
            restored_txid = "ab" * 32
            with closing(sqlite3.connect(db)) as connection:
                connection.execute(
                    "UPDATE payments SET raw=?,byte_digest=?,txid=?,stage='committed' WHERE id='cli-bounty'",
                    (restored_raw, restored_digest, restored_txid),
                )
                connection.commit()
            base, health = start()
            assert health["publication_enabled"] is False
            status, restored = http(base, "GET", "/payments/cli-bounty")
            assert status == 200 and restored["stage"] == "committed"
            assert restored["txid"] == restored_txid and restored["byte_digest"] == restored_digest
            assert http(base, "POST", "/payments/cli-bounty/retry")[0] == 503
            assert "not qualified" in cli("retry", "cli-bounty", expected=1)["error"].lower()
            stop()
            with closing(sqlite3.connect(db)) as connection:
                assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
                assert connection.execute("SELECT raw FROM payments WHERE id='cli-bounty'").fetchone()[0] == restored_raw
            assert node_requests == [], node_requests
            checks.append("restored synthetic committed row passes byte-integrity read but executable blocks HTTP/CLI publication with zero attempts and node calls")
        finally:
            stop()
            trap.shutdown()
            trap.server_close()

    result = {"test": "actual Rust HTTP/CLI qualification-gate smoke", "passed": True,
              "checks": checks, "database_counts": counts, "node_requests": len(node_requests),
              "scope": "actual service process and SQLite; no real wallet/node or payout integration"}
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("PASS:", len(checks), "Rust CLI/API checks; publication remained blocked; zero remote calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
