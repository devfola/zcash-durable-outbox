"""Forward actual JSON-RPC exactly once, then optionally lose its response.

Only numeric loopback upstream/listen addresses are accepted. The journal stores
hashes and sizes, never Authorization headers, transaction bytes, or PCZT bodies.
This is test infrastructure, not evidence that a wallet operation is retryable.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit
import uuid


def digest(data):
    return hashlib.sha256(data).hexdigest()


def loopback(host):
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("Only numeric loopback addresses are allowed")
    return host


class FaultServer(ThreadingHTTPServer):
    # A caller dying must not cancel an already-forwarded operation.
    daemon_threads = False
    allow_reuse_address = True

    def __init__(self, address, upstream, journal, method, drop_count=1, timeout=900,
                 release_file=None, release_timeout=60):
        loopback(address[0])
        parsed = urlsplit(upstream)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Upstream must be a credential-free http:// numeric loopback URL")
        loopback(parsed.hostname)
        self.upstream = parsed
        self.journal = Path(journal)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.method = method
        self.remaining = drop_count
        self.timeout = timeout
        self.release_file = None if release_file is None else Path(release_file).resolve()
        if self.release_file is not None and (self.release_file.exists() or self.release_file.is_symlink()):
            raise ValueError("Release file must not exist when the proxy starts")
        if not 0 < release_timeout <= 60:
            raise ValueError("Release timeout must be positive and at most 60 seconds")
        self.release_timeout = release_timeout
        self.lock = threading.Lock()
        super().__init__(address, Handler)

    def record(self, event, **details):
        line = json.dumps({"time_utc": datetime.now(timezone.utc).isoformat(), "event": event, **details}) + "\n"
        with self.lock:
            with self.journal.open("a", encoding="utf-8") as output:
                output.write(line)
                output.flush()
                os.fsync(output.fileno())

    def choose_drop(self, method):
        with self.lock:
            if method == self.method and self.remaining > 0:
                self.remaining -= 1
                return True
            return False

    def await_release(self, call, method):
        if self.release_file is None:
            return
        # record() fsyncs each marker before the supervisor can act on it. No
        # caller-socket check is made: caller death must not cancel the wait or
        # trigger another upstream call. Timeout still drops, never delivers.
        self.record("response_release_wait", call=call, method=method,
                    timeout_seconds=self.release_timeout)
        deadline = time.monotonic() + self.release_timeout
        while not self.release_file.is_file():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.record("response_release_timeout", call=call, method=method)
                return
            time.sleep(min(0.02, remaining))
        self.record("response_release_observed", call=call, method=method)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length_text = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or length_text is None:
            self.send_error(411)
            return
        try:
            length = int(length_text)
            if not 0 < length <= 64 * 1024 * 1024:
                raise ValueError()
        except ValueError:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            return
        try:
            request = json.loads(body)
            method = request.get("method") if isinstance(request, dict) else None
        except (ValueError, UnicodeError):
            self.send_error(400)
            return
        call = uuid.uuid4().hex
        server = self.server
        artifact = {}
        parameters = request.get("params") if isinstance(request, dict) else None
        if isinstance(parameters, list) and parameters and isinstance(parameters[0], str):
            try:
                if method == "sendrawtransaction":
                    raw = bytes.fromhex(parameters[0])
                    artifact = {"transaction_sha256": digest(raw), "transaction_size": len(raw)}
                elif method in {"pczt_inspect", "pczt_prove", "pczt_sign", "pczt_extract"}:
                    pczt = base64.b64decode(parameters[0], validate=True)
                    artifact = {"pczt_sha256": digest(pczt), "pczt_size": len(pczt)}
            except (ValueError, UnicodeError):
                artifact = {"artifact_decode_failed": True}
        server.record("forward_started", call=call, method=method,
                      request_sha256=digest(body), request_size=len(body), **artifact)
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in {"host", "content-length", "connection", "proxy-connection", "transfer-encoding"}}
        headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(server.upstream.hostname, server.upstream.port or 80, timeout=server.timeout)
        try:
            # Exactly one upstream call; transport exceptions are never retried.
            connection.request("POST", server.upstream.path or "/", body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            try:
                rpc = json.loads(response_body)
                rpc_error = bool(rpc.get("error")) if isinstance(rpc, dict) else None
            except (ValueError, UnicodeError):
                rpc_error = None
            drop = server.choose_drop(method)
            server.record("upstream_response_complete", call=call, method=method,
                          http_status=response.status, response_sha256=digest(response_body),
                          response_size=len(response_body), rpc_error=rpc_error, drop=drop)
            if drop:
                # The upstream's complete HTTP response was received and fsynced
                # before the caller is disconnected. Acceptance still needs node
                # and wallet observations; rpc_error is recorded separately.
                server.await_release(call, method)
                server.record("response_dropped", call=call, method=method)
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            try:
                self.send_response(response.status, response.reason)
                for key, value in response.getheaders():
                    if key.lower() not in {"content-length", "connection", "transfer-encoding", "server", "date"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(response_body)
                self.wfile.flush()
                server.record("response_delivered", call=call, method=method)
            except OSError as error:
                server.record("caller_disconnected", call=call, method=method, error_type=type(error).__name__)
            self.close_connection = True
        except (OSError, http.client.HTTPException) as error:
            server.record("upstream_uncertain", call=call, method=method, error_type=type(error).__name__)
            self.close_connection = True
            try:
                self.send_error(502, "Upstream outcome uncertain; not retried")
            except OSError:
                pass
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:50234")
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--drop-count", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--release-file", type=Path,
                        help="For a selected drop, wait after the fsynced upstream response until this initially absent file exists")
    parser.add_argument("--release-timeout", type=float, default=60,
                        help="Maximum release-file wait in seconds (0 < value <= 60); timeout still drops the response")
    args = parser.parse_args()
    host, port = args.listen.rsplit(":", 1)
    if args.drop_count < 0 or args.timeout <= 0:
        parser.error("drop-count must be nonnegative and timeout must be positive")
    if not 0 < args.release_timeout <= 60:
        parser.error("release-timeout must be positive and at most 60 seconds")
    server = FaultServer((host, int(port)), args.upstream, args.journal, args.method,
                         args.drop_count, args.timeout, args.release_file, args.release_timeout)
    print(json.dumps({"listen": server.server_address, "upstream": args.upstream,
                      "drop_method": args.method, "drop_count": args.drop_count,
                      "release_file_wait": args.release_file is not None,
                      "release_timeout_seconds": args.release_timeout}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Wait for already-forwarded operations. Force-killing the proxy instead
        # deliberately leaves remote completion unknown and must be reconciled.
        server.server_close()


if __name__ == "__main__":
    main()
