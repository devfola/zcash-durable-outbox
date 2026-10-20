"""Read-only source/executable manifests shared by genuine-stack qualifications.

Manifest keys are repository-relative POSIX paths. Only hashes are published;
private runtime configuration, credentials, viewing material and artifacts are
never opened here. Call once before operations, then compare the complete result
again before publishing a successful qualification summary.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _manifest(project, paths):
    result = {}
    for candidate in paths:
        path = Path(candidate).resolve(strict=True)
        if not path.is_relative_to(project) or not path.is_file():
            raise ValueError("qualification sources must be files inside the repository")
        result[path.relative_to(project).as_posix()] = _sha(path.read_bytes())
    return dict(sorted(result.items()))


def _manifest_digest(manifest):
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return _sha(canonical.encode("utf-8"))


def artifact_hashes(config, *, orchestrator=None, harness_paths=()):
    """Pin executables, full core/verifier sources, and qualification harnesses.

    Existing scalar hash names are retained. The core manifest includes every
    file under both src directories and both manifests/lockfiles, plus optional
    build scripts/toolchain/Cargo configuration. The harness manifest includes
    all top-level integration Python/shell/config files, examples, and Python
    checks in tests/scripts; generated evidence, caches and private runtime inputs
    are excluded.
    Extra harness_paths must also be repository-contained files.
    """
    project = Path(__file__).resolve().parent.parent
    orchestrator = (project / "integration" / "test_real_stack.py"
                    if orchestrator is None else Path(orchestrator).resolve(strict=True))
    core = [project / "Cargo.toml", project / "Cargo.lock",
            project / "verifier" / "Cargo.toml", project / "verifier" / "Cargo.lock"]
    for directory in (project / "src", project / "verifier" / "src"):
        if not directory.is_dir():
            raise ValueError("core and verifier source directories are required")
        core.extend(path for path in directory.rglob("*") if path.is_file())
    for base in (project, project / "verifier"):
        for name in ("build.rs", "rust-toolchain", "rust-toolchain.toml",
                     ".cargo/config", ".cargo/config.toml"):
            path = base / name
            if path.is_file():
                core.append(path)
    harness = [path for path in (project / "integration").iterdir()
               if path.is_file() and path.suffix in (".py", ".sh", ".ps1", ".cmd", ".json")]
    harness.extend(path for path in (project / "examples").iterdir()
                   if path.is_file() and path.suffix in (".py", ".json"))
    for directory in (project / "tests", project / "scripts"):
        harness.extend(path for path in directory.glob("*.py") if path.is_file())
    harness.extend((orchestrator, *harness_paths))
    source_manifest = _manifest(project, core)
    harness_manifest = _manifest(project, harness)
    result = {name: _sha(Path(path).read_bytes()) for name, path in {
        "service_sha256": config["service"],
        "verifier_sha256": config["verifier"],
        "core_cargo_lock_sha256": project / "Cargo.lock",
        "verifier_cargo_lock_sha256": project / "verifier" / "Cargo.lock",
        "orchestrator_sha256": orchestrator,
    }.items()}
    result.update({
        "manifest_schema": 1,
        "core_source_manifest": source_manifest,
        "core_source_manifest_sha256": _manifest_digest(source_manifest),
        "harness_source_manifest": harness_manifest,
        "harness_source_manifest_sha256": _manifest_digest(harness_manifest),
        "source_manifest_scope": "complete src trees, Rust manifests/locks and present build configuration",
        "harness_manifest_scope": "integration Python/shell/config sources, examples and tests/scripts Python checks",
        "private_runtime_inputs_in_manifest": False,
    })
    return result
