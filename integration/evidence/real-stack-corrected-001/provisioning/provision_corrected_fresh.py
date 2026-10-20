"""One-shot fresh sibling setup after a verified clean guest shutdown.

Never stops existing processes, edits old roots, releases owners, or retries a
failed setup. The packaged funding recipe runs only in an absent new root.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parent
NEW = Path('/home/outbox/corrected-run-001')
EVIDENCE = Path('/home/outbox/corrected-run-001-preparation-evidence')
OLD = [Path('/home/outbox/regtest'), Path('/home/outbox/final-run')]
BIN = Path('/home/outbox/bin')
RECIPES = ['provision_corrected_fresh.py', 'bootstrap-release-linux.py', 'bootstrap_registration.py',
           'configure-regtest.py', 'fund-release-regtest.py', 'pinned-sources.json']


def sha(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def save(path, value):
    with Path(path).open('x') as handle:
        json.dump(value, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())


def inventory(root):
    return {str(path.relative_to(root)): sha(path)
            for path in sorted(root.rglob('*')) if path.is_file() and not path.is_symlink()}


def assert_no_stack():
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            executable = (process / 'exe').resolve(strict=True).name
        except (FileNotFoundError, PermissionError):
            continue
        if executable in {'zebrad', 'zallet-zaino', 'zallet-zebra'}:
            raise RuntimeError('Existing stack process: refuse to stop or provision')
    for name in ('tcp', 'tcp6'):
        for line in (Path('/proc/net') / name).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A' and int(fields[1].rsplit(':', 1)[1], 16) in {29232, 29233, 50232, 50233}:
                raise RuntimeError('Existing listener: refuse to provision')


def main():
    os.umask(0o077)
    if NEW.exists() or EVIDENCE.exists() or NEW.parent != Path('/home/outbox'):
        raise RuntimeError('New disposable state/evidence already exists or escapes intended parent')
    for old in OLD:
        if not old.is_dir() or old.resolve().parent != NEW.parent:
            raise RuntimeError('Expected preserved old sibling is missing or relocated')
    assert_no_stack()
    spec = importlib.util.spec_from_file_location('bootstrap', TOOLS / 'bootstrap-release-linux.py')
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    pins = json.loads((TOOLS / 'pinned-sources.json').read_text())
    release = bootstrap.verify_existing_release(BIN, NEW.parent, pins)
    before = {str(old): inventory(old) for old in OLD}
    recipes = {name: sha(TOOLS / name) for name in RECIPES}
    EVIDENCE.mkdir(mode=0o700)
    save(EVIDENCE / 'old-state-before.json', before)
    plan = {'started_unix': time.time(), 'new_root': str(NEW),
            'old_roots_preserved': [str(path) for path in OLD],
            'old_stack_processes_present': False, 'required_ports_listening_before': False,
            'guest_boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'logical_cpus': os.cpu_count(), 'recipe_sha256': recipes,
            'verified_release': release, 'initial_funding_only': True,
            'force_kill': False, 'old_wallet_database_edits': False,
            'owner_release': False, 'automatic_retry': False}
    save(EVIDENCE / 'reviewed-plan.json', plan)
    assert_no_stack()
    NEW.mkdir(mode=0o700)
    (NEW / 'bin').symlink_to(BIN, target_is_directory=True)
    for name, expected in recipes.items():
        if sha(TOOLS / name) != expected:
            raise RuntimeError('Reviewed recipe changed before execution')
    subprocess.run([sys.executable, str(TOOLS / 'bootstrap-release-linux.py'), '--root', str(NEW),
                    '--archive-dir', str(NEW.parent), '--verify-existing'], check=True)
    with (NEW / 'funding-output.log').open('xb', buffering=0) as log:
        result = subprocess.run([sys.executable, str(TOOLS / 'fund-release-regtest.py'), '--root', str(NEW)],
                                stdout=log, stderr=subprocess.STDOUT)
    save(EVIDENCE / 'funding-result.json', {'finished_unix': time.time(),
         'exit_code': result.returncode, 'automatic_retry': False})
    after = {str(old): inventory(old) for old in OLD}
    save(EVIDENCE / 'old-state-after.json', after)
    if before != after:
        raise RuntimeError('Preserved old state differs; retain all evidence and investigate')
    result.check_returncode()
    ready = json.loads((NEW / 'regtest/ready.json').read_text())
    if ready['wallet_broadcast'] is not False or set(ready['trusted_wallet_files']) != {'sender', 'receiver'}:
        raise RuntimeError('Fresh funding readiness incomplete')
    completed = {'finished_unix': time.time(), 'fresh_funding_completed': True,
                 'new_ready_file': str(NEW / 'regtest/ready.json'),
                 'registration_indices': ready['registration_indices'],
                 'wallet_broadcast': False, 'old_state_sha256_manifest_unchanged': True,
                 'old_file_counts': {root: len(files) for root, files in before.items()},
                 'automatic_retry': False, 'owner_release': False,
                 'independent_genuine_created_pczt_verification_required': True}
    save(EVIDENCE / 'completed.json', completed)
    print(json.dumps(completed, indent=2), flush=True)


if __name__ == '__main__':
    main()
