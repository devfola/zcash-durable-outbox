#!/usr/bin/env python3
"""Replay the preserved fake-wallet baseline with a deterministic test handshake.

The original baseline is never edited. Its two-ID concurrency test uses a 0.2s
pause, shorter than process startup on some Windows hosts. Only that test's
synchronization changes; payment logic and assertions remain the same.
"""
from __future__ import annotations

import difflib
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    original = project / "reference" / "zcash-outbox-probe" / "probe.py"
    evidence = project / "evidence"
    evidence.mkdir(exist_ok=True)
    source = original.read_text(encoding="utf-8")
    changes = [
        (
            '        if self.pause and stage == "create":\n            time.sleep(self.pause)\n',
            '        if self.pause == -1 and stage == "create":\n'
            '            (self.root / "create-entered").touch()\n'
            '            deadline = time.monotonic() + 30\n'
            '            while not (self.root / "release-create").exists():\n'
            '                assert time.monotonic() < deadline, "create gate timed out"\n'
            '                time.sleep(.005)\n'
            '        elif self.pause and stage == "create":\n'
            '            time.sleep(self.pause)\n',
        ),
        ('"--intent", "invoice-1", "--pause", ".2"',
         '"--intent", "invoice-1", "--pause", "-1"'),
        (
            '        second = child(root, "--intent", "invoice-2")\n'
            '        assert json.loads(second.stdout)["status"] == "BUSY_WALLET"\n'
            '        out, err = first.communicate(timeout=30)\n',
            '        while not (root / "create-entered").exists():\n'
            '            assert time.monotonic() < deadline, "first builder did not enter create"\n'
            '            time.sleep(.005)\n'
            '        second = child(root, "--intent", "invoice-2")\n'
            '        (root / "release-create").touch()\n'
            '        assert json.loads(second.stdout)["status"] == "BUSY_WALLET"\n'
            '        out, err = first.communicate(timeout=30)\n',
        ),
    ]
    patched = source
    for before, after in changes:
        if patched.count(before) != 1:
            raise RuntimeError("Unexpected baseline source; refusing ambiguous patch")
        patched = patched.replace(before, after)
    (evidence / "baseline-synchronization.patch").write_text(
        "".join(difflib.unified_diff(source.splitlines(True), patched.splitlines(True),
                                     fromfile="preserved/probe.py", tofile="replay/probe.py")),
        encoding="utf-8",
    )
    with tempfile.TemporaryDirectory(prefix="outbox-baseline-replay-") as directory:
        replay = Path(directory) / "probe.py"
        replay.write_text(patched, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(replay), "test", "--json",
             str(evidence / "baseline-replay-synchronized.json")],
            text=True, capture_output=True,
        )
    (evidence / "baseline-replay-synchronized.log").write_text(
        result.stdout + result.stderr, encoding="utf-8"
    )
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
