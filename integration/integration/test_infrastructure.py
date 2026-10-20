"""Infrastructure checks only: these are NOT Zallet/Zebra integration tests."""
import concurrent.futures
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import tomllib
import unittest

from fault_proxy import FaultServer


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request["method"] == "slow":
            self.server.started.set()
            self.server.release.wait(5)
        with self.server.counter_lock:
            self.server.calls += 1
            result = self.server.calls
        body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass
        self.server.completed.set()


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.calls = 0
        self.upstream.counter_lock = threading.Lock()
        self.upstream.started = threading.Event()
        self.upstream.release = threading.Event()
        self.upstream.completed = threading.Event()
        self.upstream.release.set()
        self.proxy = FaultServer(("127.0.0.1", 0), f"http://127.0.0.1:{self.upstream.server_port}",
                                 Path(self.temp.name) / "journal.jsonl", "extract", 1, timeout=5)
        for server in (self.upstream, self.proxy):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def call(self, method, parameters=None, rpc_id=1):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=5)
        try:
            request = {"method": method, "id": rpc_id}
            if parameters is not None:
                request["params"] = parameters
            conn.request("POST", "/", json.dumps(request),
                         {"Content-Type": "application/json", "Authorization": "Basic synthetic-secret"})
            response = conn.getresponse()
            return json.loads(response.read())
        finally:
            conn.close()

    def journal(self):
        return [json.loads(line) for line in self.proxy.journal.read_text().splitlines()]

    def test_drop_after_actual_upstream_action_then_pass_identical_retry(self):
        with self.assertRaises(http.client.RemoteDisconnected):
            self.call("extract")
        self.assertEqual(self.upstream.calls, 1)
        self.assertEqual(self.call("extract")["result"], 2)
        records = self.journal()
        self.assertEqual(sum(r["event"] == "response_dropped" for r in records), 1)
        starts = [r for r in records if r["event"] == "forward_started"]
        self.assertEqual(starts[0]["request_sha256"], starts[1]["request_sha256"])
        self.assertNotIn("synthetic-secret", self.proxy.journal.read_text())

    def test_concurrent_calls_consume_one_drop(self):
        def request(_):
            try:
                return self.call("extract")
            except http.client.RemoteDisconnected:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(request, range(4)))
        self.assertEqual(self.upstream.calls, 4)
        self.assertEqual(replies.count(None), 1)

    def test_transaction_digest_ignores_rpc_id_and_never_logs_raw(self):
        import hashlib
        raw = b"synthetic transaction identity fixture"
        self.call("sendrawtransaction", [raw.hex()], rpc_id="first")
        self.call("sendrawtransaction", [raw.hex()], rpc_id="second")
        records = [record for record in self.journal() if record["event"] == "forward_started"]
        self.assertNotEqual(records[0]["request_sha256"], records[1]["request_sha256"])
        self.assertEqual({record["transaction_sha256"] for record in records}, {hashlib.sha256(raw).hexdigest()})
        self.assertEqual({record["transaction_size"] for record in records}, {len(raw)})
        self.assertNotIn(raw.hex(), self.proxy.journal.read_text())

    def test_remote_completes_after_caller_socket_dies(self):
        self.upstream.release.clear()
        sock = socket.create_connection(("127.0.0.1", self.proxy.server_port))
        body = json.dumps({"method": "slow", "id": 1}).encode()
        sock.sendall(f"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        self.assertTrue(self.upstream.started.wait(3))
        sock.close()
        self.upstream.release.set()
        self.assertTrue(self.upstream.completed.wait(3))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            records = self.journal()
            if any(r["event"] == "upstream_response_complete" for r in records):
                break
            time.sleep(0.01)
        self.assertEqual(self.upstream.calls, 1)
        self.assertTrue(any(r["event"] == "upstream_response_complete" for r in self.journal()))

    def test_release_handshake_retains_completed_response_after_caller_death(self):
        release_file = Path(self.temp.name) / "release-response"
        proxy = FaultServer(("127.0.0.1", 0), f"http://127.0.0.1:{self.upstream.server_port}",
                            Path(self.temp.name) / "release-journal.jsonl", "extract", 1,
                            timeout=5, release_file=release_file)
        self.proxy = proxy
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        self.addCleanup(release_file.touch)

        def wait_for(event):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if proxy.journal.exists():
                    try:
                        records = self.journal()
                    except json.JSONDecodeError:
                        records = []  # Concurrent append may still be completing.
                    if any(record["event"] == event for record in records):
                        return records
                time.sleep(0.01)
            self.fail("Journal did not contain " + event)

        caller = socket.create_connection(("127.0.0.1", proxy.server_port), timeout=5)
        self.addCleanup(caller.close)
        body = json.dumps({"method": "extract", "id": 1}).encode()
        caller.sendall(f"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        records = wait_for("response_release_wait")
        events = [record["event"] for record in records]
        self.assertLess(events.index("upstream_response_complete"), events.index("response_release_wait"))
        self.assertEqual(self.upstream.calls, 1)
        self.assertNotIn("response_dropped", events)
        # No response bytes are exposed while the supervisor's release is absent.
        caller.settimeout(0.05)
        with self.assertRaises(socket.timeout):
            caller.recv(1)
        caller.close()
        self.assertNotIn("response_dropped", [record["event"] for record in self.journal()])
        release_file.touch()
        records = wait_for("response_dropped")
        events = [record["event"] for record in records]
        self.assertLess(events.index("response_release_wait"), events.index("response_release_observed"))
        self.assertLess(events.index("response_release_observed"), events.index("response_dropped"))
        self.assertNotIn("response_delivered", events)
        self.assertNotIn("response_release_timeout", events)
        self.assertEqual(self.upstream.calls, 1)
        self.assertEqual(sum(event == "forward_started" for event in events), 1)

    def test_release_handshake_rejects_existing_file_and_excess_timeout(self):
        release_file = Path(self.temp.name) / "already-released"
        release_file.touch()
        with self.assertRaises(ValueError):
            FaultServer(("127.0.0.1", 0), f"http://127.0.0.1:{self.upstream.server_port}",
                        self.proxy.journal, "extract", release_file=release_file)
        with self.assertRaises(ValueError):
            FaultServer(("127.0.0.1", 0), f"http://127.0.0.1:{self.upstream.server_port}",
                        self.proxy.journal, "extract", release_timeout=61)

    def test_release_timeout_drops_without_delivering_or_repeating_upstream(self):
        release_file = Path(self.temp.name) / "never-released"
        proxy = FaultServer(("127.0.0.1", 0), f"http://127.0.0.1:{self.upstream.server_port}",
                            Path(self.temp.name) / "timeout-journal.jsonl", "extract", 1,
                            timeout=5, release_file=release_file, release_timeout=0.05)
        self.proxy = proxy
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        self.addCleanup(release_file.touch)
        with self.assertRaises(http.client.RemoteDisconnected):
            self.call("extract")
        events = [record["event"] for record in self.journal()]
        self.assertEqual(self.upstream.calls, 1)
        self.assertIn("response_release_timeout", events)
        self.assertLess(events.index("response_release_timeout"), events.index("response_dropped"))
        self.assertNotIn("response_release_observed", events)
        self.assertNotIn("response_delivered", events)

    def test_reject_nonloopback(self):
        with self.assertRaises(ValueError):
            FaultServer(("127.0.0.1", 0), "http://192.0.2.1:1234", self.proxy.journal, "x")


class ConfigTests(unittest.TestCase):
    def test_new_directory_consensus_parity_and_no_wallet_broadcast(self):
        spec = importlib.util.spec_from_file_location("configure_regtest", Path(__file__).with_name("configure-regtest.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "new-regtest"
            marker = module.configure(destination)
            self.assertEqual(marker["network"], "regtest")
            zebra = tomllib.loads((destination / "zebra.toml").read_text())
            for name in ("sender", "receiver"):
                wallet = tomllib.loads((destination / name / "zallet.toml").read_text())
                self.assertFalse(wallet["external"]["broadcast"])
                self.assertEqual(wallet["backend"], "zaino")
                self.assertNotIn("read_state_service", wallet["indexer"])
                self.assertEqual(len(wallet["consensus"]["regtest_nuparams"]), 10)
                self.assertEqual(zebra["network"]["testnet_parameters"]["activation_heights"]["NU6.3"], 2)
                self.assertFalse(zebra["rpc"]["enable_cookie_auth"])
            with self.assertRaises(FileExistsError):
                module.configure(destination)


if __name__ == "__main__":
    unittest.main(verbosity=2)
