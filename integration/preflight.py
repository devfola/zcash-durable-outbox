"""Read-only host checks. Exit 2 means the real-stack run is blocked."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def command(args):
    try:
        run = subprocess.run(args, capture_output=True, timeout=15)
        raw = run.stdout + run.stderr
        text = raw.decode("utf-16-le" if b"\x00" in raw else "utf-8", errors="replace").strip()
        return {"command": args, "exit_code": run.returncode, "output": text[:6000]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": args, "error": str(error)}


def inspect(bin_dir=None):
    programs = {name: shutil.which(name) for name in ("rustc", "cargo", "git", "cmake", "clang", "protoc", "docker", "wsl")}
    report = {"recorded_utc": datetime.now(timezone.utc).isoformat(), "scope": "native host only; VM state is checked by portable_vm.py status", "platform": platform.platform(),
              "python": sys.version, "programs": programs, "commands": [], "blockers": []}
    for name in ("rustc", "cargo"):
        if programs[name]:
            report["commands"].append(command([programs[name], "--version"]))
    if platform.system() != "Linux":
        report["blockers"].append("Selected official release archives target Linux. Native Windows backend compilation was not qualified; use the disposable Linux VM route or a Linux host.")
        if programs["wsl"]:
            report["commands"].append(command([programs["wsl"], "--status"]))
        if not programs["docker"]:
            report["blockers"].append("Docker CLI is absent. This native-host check does not inspect the separately provisioned QEMU guest.")
    if bin_dir:
        for name, expected_version in (("zebrad", "6.2.3"), ("zallet-zaino", "0.1.0-beta.3")):
            path = (Path(bin_dir) / name).resolve()
            if not path.is_file():
                report["blockers"].append(f"Required binary missing: {path}")
            else:
                result = command([str(path), "--version"])
                report["commands"].append(result)
                if result.get("exit_code") != 0 or expected_version not in result.get("output", ""):
                    report["blockers"].append(f"Binary version probe failed or differs from expected {expected_version}: {path}")
    else:
        report["blockers"].append("No pinned binary directory supplied; provide --bin-dir after a successful source build.")
    report["runtime_qualified"] = False
    report["status"] = "blocked" if report["blockers"] else "ready-for-runtime-qualification"
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = inspect(args.bin_dir)
    encoded = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    raise SystemExit(2 if report["blockers"] else 0)
