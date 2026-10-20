#!/usr/bin/env python3
"""Controlled Linux wallet restart during one dispatched pczt_prove RPC.

Qualification only: no create, sign, extract, publish, database edit, or owner
release. Keep the complete evidence directory private. A sent-request marker
proves socket dispatch, not entry into a specific internal prover instruction.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import tomllib
import urllib.parse


def save(path: Path, value):
    data = json.dumps(value, indent=2).encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def credential(config):
    entries = config["rpc"]["auth"]
    if len(entries) != 1 or not isinstance(entries[0].get("password"), str):
        raise ValueError("this helper requires the disposable release plaintext RPC auth entry")
    return entries[0]["user"] + ":" + entries[0]["password"]


def endpoint(config):
    bindings = config["rpc"]["bind"]
    if len(bindings) != 1:
        raise ValueError("one dedicated loopback RPC binding is required")
    address = urllib.parse.urlsplit("http://" + bindings[0])
    if not ipaddress.ip_address(address.hostname).is_loopback or not address.port:
        raise ValueError("RPC binding must be numeric loopback")
    return address.hostname, address.port


def call(config, method, params=None, timeout=30):
    host, port = endpoint(config)
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": method, "method": method, "params": params or []})
        connection.request("POST", "/", body=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(credential(config).encode()).decode(),
        })
        response = connection.getresponse()
        result = json.load(response)
        if response.status != 200 or result.get("error"):
            raise RuntimeError("RPC failed: " + method)
        return result["result"]
    finally:
        connection.close()


def process_identity(pid: int, expected: list[str]):
    process = Path("/proc") / str(pid)
    if process.stat().st_uid != os.getuid():
        raise RuntimeError("wallet process is not owned by this guest user")
    actual = [item.decode() for item in (process / "cmdline").read_bytes().split(b"\0") if item]
    if actual != expected or (process / "exe").resolve() != Path(expected[0]).resolve():
        raise RuntimeError("wallet executable/config command differs from the exact owned process record")
    fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "start_ticks": fields[19], "cpu_ticks": int(fields[11]) + int(fields[12])}


def stopped(pid, start_ticks):
    process = Path("/proc") / str(pid)
    try:
        fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[19] != start_ticks:
            raise RuntimeError("PID was reused; refuse restart")
        return fields[0] == "Z"
    except FileNotFoundError:
        return True


def prove_client(config_path: Path, pczt: Path, evidence: Path):
    config = tomllib.loads(config_path.read_text())
    artifact = pczt.read_bytes()
    host, port = endpoint(config)
    connection = http.client.HTTPConnection(host, port, timeout=900)
    save(evidence / "prove.request-started.json", {
        "method": "pczt_prove", "pczt_sha256": hashlib.sha256(artifact).hexdigest(), "time": time.time(),
    })
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": "restart-prove", "method": "pczt_prove",
                           "params": [base64.b64encode(artifact).decode()]})
        connection.request("POST", "/", body=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(credential(config).encode()).decode(),
        })
        save(evidence / "prove.request-sent.json", {"time": time.time(), "method": "pczt_prove",
             "meaning": "HTTP request bytes sent to owned wallet socket; internal instruction is not instrumented"})
        response = connection.getresponse()
        payload = response.read()
        result = json.loads(payload)
        save(evidence / "prove.response.private.json", result)
        save(evidence / "prove.client-outcome.json", {"time": time.time(), "response_received": True,
             "http_status": response.status, "rpc_success": response.status == 200 and not result.get("error"),
             "response_sha256": hashlib.sha256(payload).hexdigest(), "replayed": False})
    except Exception as error:
        save(evidence / "prove.client-outcome.json", {"time": time.time(), "response_received": False,
             "error_type": type(error).__name__, "outcome": "uncertain", "replayed": False})
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/home/outbox/regtest"))
    parser.add_argument("--role", choices=("sender", "receiver"), required=True)
    parser.add_argument("--pczt", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=0.2, help="Seconds after sent marker before stopping (0..5)")
    parser.add_argument("--stop-method", choices=("sigterm", "rpc"), default="sigterm")
    args = parser.parse_args()
    if sys.platform != "linux" or not 0 <= args.delay <= 5:
        parser.error("requires Linux and --delay between 0 and 5 seconds")
    os.umask(0o077)
    root = args.root.resolve(strict=True)
    wallet_dir = (root / args.role).resolve(strict=True)
    if wallet_dir.parent != root:
        raise ValueError("wallet directory escapes selected root")
    config_path = wallet_dir / "zallet.toml"
    config_bytes = config_path.read_bytes()
    config = tomllib.loads(config_bytes.decode())
    if config.get("backend") != "zaino" or config["consensus"]["network"] != "regtest" or config["external"]["broadcast"] is not False:
        raise RuntimeError("requires disposable regtest zaino wallet with broadcast=false")
    expected_params = {"5ba81b19:1", "76b809bb:1", "2bb40e60:1", "f5b9230b:1", "e9ff75a6:1",
                       "c2d6d0b4:2", "c8e71055:2", "4dec4df0:2", "5437f330:2", "37a5165b:2"}
    if set(config["consensus"]["regtest_nuparams"]) != expected_params:
        raise RuntimeError("unexpected upgrade profile")
    endpoint(config)
    credential(config)
    process_file = root / (args.role + "-process.json")
    record = json.loads(process_file.read_text())
    expected = [str((root.parent / "bin" / "zallet-zaino").resolve(strict=True)), "-d", str(wallet_dir), "start"]
    if record["command"] != expected or type(record["pid"]) is not int or record["pid"] <= 1:
        raise RuntimeError("process record is not the exact expected disposable wallet command")
    old = process_identity(record["pid"], expected)
    pczt = args.pczt.resolve(strict=True)
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, mode=0o700, exist_ok=False)
    # This directory and immutable marker are retained even on any exception.
    save(evidence / "experiment-inflight.json", {"method": "pczt_prove", "role": args.role,
         "pczt_sha256": hashlib.sha256(pczt.read_bytes()).hexdigest(), "owner_release_performed": False,
         "automatic_replay_allowed": False, "time": time.time()})
    before = {"identity": old, "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
              "broadcast": False, "wallet": call(config, "getwalletinfo"), "accounts": call(config, "z_listaccounts"),
              "balances": call(config, "z_gettotalbalance", [1, True]), "notes": call(config, "z_listunspent")}
    save(evidence / "before.private.json", before)
    client = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_client", str(config_path),
                               str(pczt), str(evidence)], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    save(evidence / "client-process.json", {"pid": client.pid, "method": "pczt_prove"})
    deadline = time.monotonic() + 10
    while not (evidence / "prove.request-sent.json").exists():
        if client.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("dispatch marker not observed; preserve evidence and do not replay")
        time.sleep(0.01)
    time.sleep(args.delay)
    if client.poll() is not None or (evidence / "prove.client-outcome.json").exists():
        raise RuntimeError("prove already completed; no during-operation restart is claimed")
    identity = process_identity(old["pid"], expected)
    if identity["start_ticks"] != old["start_ticks"] or config_path.read_bytes() != config_bytes:
        raise RuntimeError("process/config changed; do not signal")
    save(evidence / "stop.started.json", {"time": time.time(), "method": args.stop_method,
         "request_sent_marker_observed": True, "client_outcome_present": False, "identity": identity})
    if args.stop_method == "rpc":
        save(evidence / "stop.response.json", {"result": call(config, "stop"), "time": time.time()})
    else:
        os.kill(old["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 60
    while not stopped(old["pid"], old["start_ticks"]):
        if time.monotonic() > deadline:
            raise RuntimeError("owned wallet did not stop; no forced kill or duplicate restart")
        time.sleep(0.1)
    save(evidence / "stop.observed.json", {"time": time.time(), "old_process_stopped": True})
    try:
        client.wait(timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("caller outcome remains uncertain; preserve evidence and do not restart automatically")
    if config_path.read_bytes() != config_bytes:
        raise RuntimeError("config changed; refuse restart")
    with (root / (args.role + ".log")).open("ab", buffering=0) as log:
        child = subprocess.Popen(expected, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
    new_record = {"pid": child.pid, "command": expected, "started": time.time()}
    save(evidence / "restarted-process.json", new_record)
    # Replace only the owned process record after preserving the old record in evidence.
    save(evidence / "old-process-record.json", record)
    temporary_record = root / (args.role + "-process.restart-new.json")
    save(temporary_record, new_record)
    os.replace(temporary_record, process_file)
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    deadline = time.monotonic() + 120
    while True:
        if child.poll() is not None:
            raise RuntimeError("restarted wallet exited; retain evidence and ownership")
        try:
            after = {"wallet": call(config, "getwalletinfo"), "accounts": call(config, "z_listaccounts"),
                     "balances": call(config, "z_gettotalbalance", [1, True]), "notes": call(config, "z_listunspent")}
            break
        except (OSError, ValueError, RuntimeError):
            if time.monotonic() > deadline:
                raise RuntimeError("restart readiness uncertain; retain evidence and ownership")
            time.sleep(0.5)
    save(evidence / "after.private.json", after)
    outcome = json.loads((evidence / "prove.client-outcome.json").read_text())
    save(evidence / "result.json", {"actual_wallet_restart": True,
         "client_completion_not_observed_when_stop_requested": True,
         "internal_prover_instruction_instrumented": False, "request_sent_time": json.loads((evidence / "prove.request-sent.json").read_text())["time"],
         "stop_requested_time": json.loads((evidence / "stop.started.json").read_text())["time"],
         "client_outcome": outcome, "same_config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest() == before["config_sha256"],
         "same_accounts_after_restart": after["accounts"] == before["accounts"],
         "same_balance_after_restart": after["balances"] == before["balances"],
         "same_unspent_notes_after_restart": sorted(json.dumps(note, sort_keys=True) for note in after["notes"]) == sorted(json.dumps(note, sort_keys=True) for note in before["notes"]),
         "new_process_identity_verified": bool(process_identity(child.pid, expected)), "broadcast_before": False,
         "broadcast_after": False, "database_edits_performed": False, "pczt_replayed": False,
         "extraction_performed": False, "publication_performed": False, "owner_release_performed": False,
         "inflight_marker_retained": True, "automatic_recovery_qualified": False})
    print("Controlled prove/restart observation complete; retain evidence and construction ownership.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_client":
        prove_client(*(Path(value) for value in sys.argv[2:]))
    else:
        main()
