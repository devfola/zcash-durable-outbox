"""Nonpublishing real-process PCZT qualification recorder. No mock backend.

Uses a new evidence directory and a persistent exclusive experiment lock. A lost
response or caller death requires investigation; this script never resumes or
replays an uncertain operation. It does not implement payment-effect approval.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request
import uuid


def local_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "http" or not ipaddress.ip_address(parsed.hostname or "").is_loopback
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or parsed.port is None):
        raise ValueError("RPC endpoints must be numeric HTTP loopback origins")
    return value.rstrip("/")


def save(path: Path, data: bytes) -> None:
    """Create and fsync completed evidence; never overwrite a stage artifact."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if hasattr(os, "O_DIRECTORY"):
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def save_json(path: Path, value) -> None:
    save(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


class Rpc:
    def __init__(self, url: str, credentials: str | None, timeout: int):
        self.url, self.timeout = local_origin(url), timeout
        self.authorization = ("Basic " + base64.b64encode(credentials.strip().encode()).decode()) if credentials else None
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method: str, parameters):
        identifier = str(uuid.uuid4())
        payload = json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": parameters}).encode()
        headers = {"Content-Type": "application/json"}
        if self.authorization:
            headers["Authorization"] = self.authorization
        request = urllib.request.Request(self.url, data=payload, headers=headers)
        # Exactly one application call; no retry after transport/response errors.
        with self.client.open(request, timeout=self.timeout) as response:
            body = response.read(16 * 1024 * 1024 + 1)
        if len(body) > 16 * 1024 * 1024:
            raise RuntimeError("RPC response exceeds limit; outcome unknown")
        value = json.loads(body)
        if value.get("id") != identifier or value.get("error") is not None or "result" not in value:
            code = (value.get("error") or {}).get("code")
            raise RuntimeError(f"{method} response invalid/error code {code}; outcome requires reconciliation")
        return value["result"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


REGTEST_GENESIS = "029f11d80ef9765602235e1bc9727e3eb6ba20839319f761fee920d63401e327"
REGTEST_UPGRADES = {
    "5ba81b19": 1, "76b809bb": 1, "2bb40e60": 1, "f5b9230b": 1,
    "e9ff75a6": 1, "c2d6d0b4": 2, "c8e71055": 2, "4dec4df0": 2,
    "5437f330": 2, "37a5165b": 2,
}


def qualified_chain(node: Rpc):
    chain = node.call("getblockchaininfo", [])
    genesis = node.call("getblockhash", [0])
    # Zebra 6.2.3 uses NetworkKind::bip70_network_name, which returns "test"
    # for both public testnet and regtest. Bind the distinct regtest genesis AND
    # our exact early activation schedule; the label alone is not a guard.
    upgrades = chain.get("upgrades", {})
    if (chain.get("chain") != "test" or genesis != REGTEST_GENESIS
            or set(upgrades) != set(REGTEST_UPGRADES)
            or any(upgrades[key].get("activationheight") != height
                   or upgrades[key].get("status") != "active"
                   for key, height in REGTEST_UPGRADES.items())
            or chain.get("consensus", {}).get("nextblock") != "37a5165b"):
        raise RuntimeError("Refusing node outside pinned NU6.3 regtest profile")
    return chain, genesis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-url", default="http://127.0.0.1:29232")
    parser.add_argument("--wallet-url", default="http://127.0.0.1:50232")
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--account", type=uuid.UUID, required=True)
    parser.add_argument("--wallet", default="disposable-sender")
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--amount-zat", type=int, required=True)
    parser.add_argument("--memo-hex", default="")
    parser.add_argument("--max-fee-zat", type=int, required=True)
    parser.add_argument("--expected-branch", default="37a5165b")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--wallet-lock", type=Path, required=True,
                        help="Same persistent lock path for every experiment using this dedicated wallet")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if not 0 < args.amount_zat <= 2_100_000_000_000_000 or args.max_fee_zat <= 0:
        parser.error("positive integer zatoshi amount and fee ceiling required")
    if not re.fullmatch(r"(?:[a-fA-F0-9]{2}){0,512}", args.memo_hex):
        parser.error("memo must encode at most 512 whole bytes")
    if not args.recipient.startswith("uregtest1") or not re.fullmatch(r"[a-f0-9]{8}", args.expected_branch):
        parser.error("regtest unified recipient and 8-digit branch required")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", args.wallet):
        parser.error("wallet name must use the outbox ID syntax")
    if not 1 <= args.timeout <= 3600:
        parser.error("timeout must be 1..3600 seconds")
    node = Rpc(args.node_url, None, args.timeout)
    wallet = Rpc(args.wallet_url, args.credential_file.read_text(), args.timeout)
    chain, genesis = qualified_chain(node)
    args.evidence_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    save_json(args.wallet_lock, {"experiment": str(args.evidence_dir.resolve()), "owner": str(uuid.uuid4()),
                                 "policy": "retained until qualified reconciliation; never lease-steal"})
    save_json(args.evidence_dir / "node-before.json", chain)
    save_json(args.evidence_dir / "genesis.json", genesis)
    approved = {"network": "regtest", "account": str(args.account), "recipient": args.recipient,
                "amount_zat": args.amount_zat, "memo_hex": args.memo_hex.lower(),
                "max_fee_zat": args.max_fee_zat, "privacy_policy": "FullPrivacy",
                "expected_branch": args.expected_branch, "publication": "prohibited"}
    save_json(args.evidence_dir / "experiment.json", approved)
    tip = chain["blocks"]
    intent = {"network": "regtest", "wallet": args.wallet, "account": str(args.account),
              "recipient": {"address": args.recipient, "amount_zat": args.amount_zat,
                            "memo_hex": args.memo_hex.lower()},
              "fee": {"rule": "zip317", "max_fee_zat": args.max_fee_zat},
              "expiry": {"min_height": tip + 1, "max_height": tip + 100}, "privacy": "FullPrivacy"}
    save_json(args.evidence_dir / "intent.json", intent)

    def operation(stage, method, parameters):
        save_json(args.evidence_dir / f"{stage}.started.json", {"method": method, "time_unix": time.time(),
                   "request_digest": hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()})
        result = wallet.call(method, parameters)
        save_json(args.evidence_dir / f"{stage}.completed.json", result)
        return result

    def inspect(stage, pczt):
        value = operation(f"{stage}-inspect", "pczt_inspect", [pczt])
        if (value.get("consensus_branch_id") != args.expected_branch
                or value.get("privacy_policy") != "FullPrivacy" or value.get("wallet_created") is not True
                or type(value.get("fee_zat")) is not int or not 0 <= value["fee_zat"] <= args.max_fee_zat
                or not intent["expiry"]["min_height"] <= value.get("expiry_height", 0) <= intent["expiry"]["max_height"]
                or value.get("transparent", {}).get("inputs") or value.get("transparent", {}).get("outputs")):
            raise RuntimeError("Preliminary RPC metadata check failed; retain experiment and wallet lock")
        # This is metadata screening ONLY. Memos/change/recipient commitments still
        # require independent verification before any publication can be authorized.
        return value

    amount = f"{args.amount_zat // 100_000_000}.{args.amount_zat % 100_000_000:08d}"
    recipient = {"address": args.recipient, "amount": amount}
    # Omission is Zcash's no-memo encoding (0xf6 + padding), whereas an explicit
    # empty hex string would encode an empty text memo (all zeroes).
    if args.memo_hex:
        recipient["memo"] = args.memo_hex.lower()
    try:
        created = operation("create", "pczt_create", [str(args.account), [recipient], 1, "FullPrivacy", "orchard"])
        pczt = created["pczt"]
        save(args.evidence_dir / "created.pczt", base64.b64decode(pczt, validate=True))
        inspect("created", pczt)
        pczt = operation("prove", "pczt_prove", [pczt])["pczt"]
        save(args.evidence_dir / "proven.pczt", base64.b64decode(pczt, validate=True))
        inspect("proven", pczt)
        pczt = operation("sign", "pczt_sign", [pczt, "FullPrivacy", True])["pczt"]
        save(args.evidence_dir / "signed.pczt", base64.b64decode(pczt, validate=True))
        inspect("signed", pczt)
        extracted = operation("extract", "pczt_extract", [pczt])
        raw = bytes.fromhex(extracted["hex"])
        save(args.evidence_dir / "final-transaction.bin", raw)
        save_json(args.evidence_dir / "result.json", {"raw_sha256": hashlib.sha256(raw).hexdigest(),
                  "wallet_reported_txid": extracted["txid"], "wallet_reported_stored": extracted["stored"],
                  "independent_payment_effects_verified": False, "submitted": False,
                  "construction_lock_retained": True})
        if extracted.get("stored") is not True:
            raise RuntimeError("Wallet did not report recording extraction; retain lock and investigate")
        print("Real RPC pipeline returned artifacts; not independently approved or submitted. Lock retained.")
    except Exception as error:
        save_json(args.evidence_dir / "quarantined.json", {"reason_type": type(error).__name__,
                  "policy": "no replay or lease takeover; inspect saved operation evidence"})
        raise


if __name__ == "__main__":
    main()
