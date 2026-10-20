"""One real RPC, killed caller, completed upstream response then dropped response.

Requires an existing saved PCZT from the dedicated nonpublishing qualification.
This is an explicit boundary experiment, never automatic payment recovery.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from fault_proxy import FaultServer
from qualify_pczt import Rpc, save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=["pczt_prove", "pczt_extract"], required=True)
    parser.add_argument("--pczt", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--wallet-url", default="http://127.0.0.1:50232")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=False)
    pczt = base64.b64encode(args.pczt.read_bytes()).decode()
    credential = args.credential_file.read_text()
    journal = args.evidence_dir / "proxy.jsonl"
    release = args.evidence_dir / "release"
    server = FaultServer(("127.0.0.1", 0), args.wallet_url, journal,
                         args.method, 1, args.timeout, release, 60)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    # Credential and artifact travel over stdin, never process command line/logs.
    source = ("import json,sys; from qualify_pczt import Rpc; "
              "x=json.load(sys.stdin); Rpc(x['url'],x['credential'],x['timeout'])"
              ".call(x['method'],[x['pczt']])")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    child = subprocess.Popen([sys.executable, "-c", source], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=Path(__file__).resolve().parent, creationflags=flags)
    payload = dict(url=f"http://127.0.0.1:{server.server_port}", credential=credential,
                   timeout=args.timeout, method=args.method, pczt=pczt)
    child.stdin.write(json.dumps(payload).encode())
    child.stdin.close()
    deadline = time.monotonic() + args.timeout
    killed_before_completion = None
    try:
        while time.monotonic() < deadline:
            entries = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
            if killed_before_completion is None and any(e["event"] == "forward_started" for e in entries):
                killed_before_completion = not any(e["event"] == "upstream_response_complete" for e in entries)
                child.kill()
                child.wait(timeout=10)
                save_json(args.evidence_dir / "caller-death.json", {
                    "method": args.method, "caller_exit_code": child.returncode,
                    "killed_before_upstream_completion": killed_before_completion,
                    "time_unix": time.time()})
            completed = [e for e in entries if e["event"] == "upstream_response_complete"]
            if completed:
                save_json(args.evidence_dir / "result.json", {
                    "method": args.method, "genuine_wallet_rpc": True,
                    "caller_killed": killed_before_completion is not None,
                    "killed_before_upstream_completion": killed_before_completion,
                    "upstream_completed_successfully": completed[0]["rpc_error"] is False,
                    "pczt_sha256": hashlib.sha256(args.pczt.read_bytes()).hexdigest(),
                    "response_delivered_to_caller": False,
                    "publication_performed": False, "automatic_recovery_qualified": False})
                release.touch(exist_ok=False)
                print("Real RPC completed after caller death; response dropped; no automatic recovery.")
                break
            if any(e["event"] == "upstream_uncertain" for e in entries):
                raise RuntimeError("Upstream outcome uncertain; inspect evidence")
            time.sleep(.01)
        else:
            raise TimeoutError("Remote completion not observed; retain ownership")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if not release.exists():
            release.touch()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
