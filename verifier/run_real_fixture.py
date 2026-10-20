"""Run the offline helper on genuine saved artifacts and altered expectations.

This script performs no RPC/wallet/chain operations and never copies viewing keys
or PCZT/raw data to its sanitized evidence directory.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--trusted-wallet", type=Path, required=True)
    parser.add_argument("--tip-height", type=int, required=True)
    parser.add_argument("--repeat-raw", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    args = parser.parse_args()
    args.scratch_dir.mkdir(parents=True, exist_ok=True)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    intent_path = args.fixture / "intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    trusted = json.loads(args.trusted_wallet.read_text(encoding="utf-8"))
    raw_path = args.fixture / "final-transaction.bin"
    environment = dict(os.environ, RAYON_NUM_THREADS="2")
    cases = []

    def run(name, pczt, approved=intent_path, raw=None, effects_only=False, success=True, tip=None):
        command = [str(args.binary.resolve()), "--intent", str(approved.resolve()),
                   "--pczt", str(pczt.resolve()), "--trusted-wallet", str(args.trusted_wallet.resolve()),
                   "--tip-height", str(args.tip_height if tip is None else tip)]
        if raw is not None:
            command += ["--raw", str(raw.resolve())]
        if effects_only:
            command += ["--effects-only"]
        start = time.monotonic()
        completed = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=180)
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        try:
            report = json.loads(stream)
        except json.JSONDecodeError:
            report = {"invalid_helper_report": True}
        observed = completed.returncode == 0
        passed = observed == success and report.get("publication_enabled") is False
        passed = passed and report.get("live_adapter_qualified") is False
        if observed:
            passed = passed and report.get("artifact_effects_verified") is True
            passed = passed and report.get("authorization_verified") == (not effects_only)
            passed = passed and report.get("raw_response_verified") == (raw is not None)
        else:
            passed = passed and report.get("artifact_effects_verified") is False
            passed = passed and report.get("authorization_verified") is False
        item = {"case": name, "expected_success": success, "exit_code": completed.returncode,
                "passed": passed, "elapsed_seconds": round(time.monotonic() - start, 3), "report": report}
        cases.append(item)
        (args.evidence_dir / (name + ".json")).write_text(json.dumps(item, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"case": name, "passed": passed, "exit_code": completed.returncode}), flush=True)
        return report

    for stage in ("created", "proven", "signed"):
        run(stage + "-effects", args.fixture / (stage + ".pczt"), effects_only=True)
    first = run("signed-original-raw", args.fixture / "signed.pczt", raw=raw_path)
    second = run("signed-repeated-extraction-raw", args.fixture / "signed.pczt", raw=args.repeat_raw)

    mutations = {
        "changed-recipient": lambda v: v["recipient"].update(address=trusted["registration_address"]),
        "changed-amount": lambda v: v["recipient"].update(amount_zat=v["recipient"]["amount_zat"] + 1),
        "changed-memo": lambda v: v["recipient"].update(memo_hex="00"),
        "no-memo-is-not-user-memo": lambda v: v["recipient"].update(memo_hex=""),
        "fee-cap-too-low": lambda v: v["fee"].update(max_fee_zat=1),
        "changed-fee-policy": lambda v: v["fee"].update(rule="fixed"),
        "changed-privacy": lambda v: v.update(privacy="NoPrivacy"),
        "changed-expiry": lambda v: v["expiry"].update(min_height=1, max_height=2),
        "changed-account": lambda v: v.update(account="00000000-0000-4000-8000-000000000001"),
    }
    for name, mutate in mutations.items():
        altered = copy.deepcopy(intent)
        mutate(altered)
        altered_path = args.scratch_dir / (name + ".json")
        altered_path.write_text(json.dumps(altered), encoding="utf-8")
        run(name, args.fixture / "signed.pczt", approved=altered_path, effects_only=True, success=False)

    run("expiry-at-next-block", args.fixture / "signed.pczt", effects_only=True, success=False,
        tip=first["effects"]["expiry_height"] - 1)
    run("unsigned-rejects-completed-mode", args.fixture / "created.pczt", success=False)

    trailing = args.scratch_dir / "trailing-raw.bin"
    trailing.write_bytes(raw_path.read_bytes() + b"\x00")
    run("reject-raw-trailing-byte", args.fixture / "signed.pczt", raw=trailing, success=False)
    corrupted = args.scratch_dir / "corrupt-binding-signature.bin"
    data = bytearray(raw_path.read_bytes())
    # In the pinned V6 encoding the final field is Ironwood's binding signature.
    # Changing it exercises actual BatchValidator rejection, beyond txid equality.
    data[-1] ^= 1
    corrupted.write_bytes(data)
    run("reject-raw-binding-signature", args.fixture / "signed.pczt", raw=corrupted, success=False)

    repeat_relation = {
        "same_independent_consensus_txid": first.get("consensus_txid") == second.get("consensus_txid"),
        "different_actual_raw_sha256": first.get("raw_sha256") != second.get("raw_sha256"),
        "both_raw_responses_verified": first.get("raw_response_verified") is True and second.get("raw_response_verified") is True,
    }
    summary = {"real_fixture": True, "wallet_operations_performed_by_runner": False,
               "publication_enabled": False, "live_adapter_qualified": False,
               "helper_sha256": digest(args.binary), "helper_lock_sha256": digest(Path(__file__).parent / "Cargo.lock"),
               "input_sha256": {stage: digest(args.fixture / (stage + ".pczt")) for stage in ("created", "proven", "signed")},
               "tip_height_supplied": args.tip_height, "repeat_extraction_relation": repeat_relation,
               "passed": all(case["passed"] for case in cases) and all(repeat_relation.values()),
               "cases": cases}
    (args.evidence_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(cases), "passed": summary["passed"], "repeat_relation": repeat_relation}), flush=True)
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
