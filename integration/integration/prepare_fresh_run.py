#!/usr/bin/env python3
"""Read-only plan, or explicit one-shot graceful stop and new disposable funding.

Old wallet/outbox directories and construction claims are never deleted, moved,
edited or reused. --execute stops only exactly validated owned processes, leaves
all evidence on uncertainty, and provisions a distinct new sibling root. There
is no force kill or automatic restart/replay of a failed preparation.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import tomllib


HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
SOURCE_FILES = ['prepare_fresh_run.py', 'bootstrap-release-linux.py', 'bootstrap_registration.py',
                'configure-regtest.py', 'fund-release-regtest.py', 'pinned-sources.json']


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def save(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(value, output, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def identity(pid, command):
    process = Path('/proc') / str(pid)
    if type(pid) is not int or pid <= 1 or process.stat().st_uid != os.getuid():
        raise RuntimeError('Process PID/user does not match this disposable operator')
    observed = [entry.decode() for entry in (process / 'cmdline').read_bytes().split(b'\0') if entry]
    if observed != command or (process / 'exe').resolve() != Path(command[0]).resolve():
        raise RuntimeError('Executable or exact config command differs; refuse process operation')
    fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise RuntimeError('Expected an active owned process; inspect an already stopped run manually')
    return {'pid': pid, 'start_ticks': fields[19], 'command': command}


def listener_inodes(port):
    result = set()
    for name in ('tcp', 'tcp6'):
        for line in (Path('/proc/net') / name).read_text().splitlines()[1:]:
            fields = line.split()
            if int(fields[1].rsplit(':', 1)[1], 16) == port and fields[3] == '0A':
                result.add(fields[9])
    return result


def require_owned_listener(pid, port):
    owned = set()
    for descriptor in (Path('/proc') / str(pid) / 'fd').iterdir():
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        if target.startswith('socket:['):
            owned.add(target[8:-1])
    actual = listener_inodes(port)
    if not actual or not actual.issubset(owned):
        raise RuntimeError(f'Port {port} is not exclusively listened on by the exact owned PID')


def process_stopped(snapshot):
    process = Path('/proc') / str(snapshot['pid'])
    try:
        fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError:
        return True
    if fields[19] != snapshot['start_ticks']:
        raise RuntimeError('PID reused; refuse to infer a safe shutdown')
    return fields[0] == 'Z'


def stop_wallet(config):
    entries = config['rpc']['auth']
    if len(entries) != 1 or not isinstance(entries[0].get('password'), str):
        raise RuntimeError('Expected disposable plaintext RPC auth schema')
    userpass = entries[0]['user'] + ':' + entries[0]['password']
    host, port = config['rpc']['bind'][0].rsplit(':', 1)
    connection = http.client.HTTPConnection(host, int(port), timeout=30)
    try:
        connection.request('POST', '/', json.dumps({'jsonrpc': '2.0', 'id': 'fresh-stop',
            'method': 'stop', 'params': []}), {'Content-Type': 'application/json',
            'Authorization': 'Basic ' + base64.b64encode(userpass.encode()).decode()})
        response = connection.getresponse()
        value = json.load(response)
        if response.status != 200 or value.get('error'):
            raise RuntimeError('Stop RPC did not return success; never replay automatically')
        return {'rpc_response_received': True}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-root', type=Path, default=Path('/home/outbox/regtest'))
    parser.add_argument('--new-root', type=Path, default=Path('/home/outbox/final-run'))
    parser.add_argument('--bin-dir', type=Path, default=Path('/home/outbox/bin'))
    parser.add_argument('--archive-dir', type=Path, default=Path('/home/outbox'))
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('This preparation requires the Linux guest')
    old = args.old_root.resolve(strict=True)
    new = args.new_root.resolve()
    binaries = args.bin_dir.resolve(strict=True)
    archives = args.archive_dir.resolve(strict=True)
    evidence = new.with_name(new.name + '-preparation-evidence')
    if new.exists() or evidence.exists() or new.parent != old.parent or new == old:
        raise RuntimeError('New root/evidence must be absent, distinct siblings of the preserved old state')
    pins = json.loads((HERE / 'pinned-sources.json').read_text())
    source_hashes = {name: digest(HERE / name) for name in SOURCE_FILES}
    spec = importlib.util.spec_from_file_location('release_bootstrap', HERE / 'bootstrap-release-linux.py')
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    release = bootstrap.verify_existing_release(binaries, archives, pins)
    snapshots, configs, config_hashes = {}, {}, {}
    for role in ('sender', 'receiver', 'node'):
        config_path = old / ('zebra.toml' if role == 'node' else role + '/zallet.toml')
        configs[role] = tomllib.loads(config_path.read_text())
        config_hashes[role] = digest(config_path)
        config = configs[role]
        command = [str(binaries / ('zebrad' if role == 'node' else 'zallet-zaino')),
                   '-c' if role == 'node' else '-d', str(config_path if role == 'node' else old / role), 'start']
        record = json.loads((old / (role + '-process.json')).read_text())
        if record['command'] != command:
            raise RuntimeError('Owned process record differs from exact expected command: ' + role)
        snapshots[role] = identity(record['pid'], command)
        if record.get('start_ticks', snapshots[role]['start_ticks']) != snapshots[role]['start_ticks']:
            raise RuntimeError('Process record start ticks no longer match: ' + role)
        if role == 'node':
            if config['network']['network'] != 'Regtest' or config['rpc']['listen_addr'] != '127.0.0.1:29232':
                raise RuntimeError('Unexpected old disposable node profile')
            ports = (29232, 29233)
        else:
            port = 50232 if role == 'sender' else 50233
            if (config.get('backend') != 'zaino' or config['consensus']['network'] != 'regtest'
                    or config['external']['broadcast'] is not False or config['rpc']['bind'] != [f'127.0.0.1:{port}']):
                raise RuntimeError('Unexpected old nonpublishing disposable wallet configuration')
            ports = (port,)
        for port in ports:
            require_owned_listener(record['pid'], port)
    plan = {'execute': args.execute, 'old_root_preserved': str(old), 'new_root': str(new),
            'preparation_evidence': str(evidence), 'owned_processes': snapshots,
            'old_config_sha256': config_hashes, 'recipe_sha256': source_hashes,
            'verified_release': release, 'stop_order': ['sender', 'receiver', 'node'],
            'force_kill': False, 'wallet_database_edits': False, 'claim_release': False,
            'host_outbox_and_proxy_mutators_must_be_stopped_before_execute': True,
            'new_bin_symlink': str(binaries), 'ports_reused_only_after_old_listeners_stop': True}
    print(json.dumps(plan, indent=2), flush=True)
    if not args.execute:
        return
    os.umask(0o077)
    evidence.mkdir(mode=0o700)
    save(evidence / 'reviewed-plan.json', plan)
    for role in ('sender', 'receiver', 'node'):
        snapshot = snapshots[role]
        config_path = old / ('zebra.toml' if role == 'node' else role + '/zallet.toml')
        current = identity(snapshot['pid'], snapshot['command'])
        if current['start_ticks'] != snapshot['start_ticks'] or digest(config_path) != config_hashes[role]:
            raise RuntimeError('Process/config changed after planning; do not stop or replay')
        save(evidence / (role + '-stop.started.json'), {'time': time.time(), 'identity': current,
             'method': 'SIGTERM' if role == 'node' else 'RPC stop once'})
        if role == 'node':
            os.kill(snapshot['pid'], signal.SIGTERM)
        else:
            try:
                outcome = stop_wallet(configs[role])
            except Exception as error:
                outcome = {'rpc_response_received': False, 'outcome': 'uncertain', 'error_type': type(error).__name__}
            save(evidence / (role + '-stop.response.json'), outcome)
        deadline = time.monotonic() + 90
        while not process_stopped(snapshot):
            if time.monotonic() > deadline:
                raise RuntimeError('Owned process has not stopped; no force kill or duplicate action')
            time.sleep(0.1)
        save(evidence / (role + '-stopped.json'), {'time': time.time(), 'observed_stopped': True})
    for port in (29232, 29233, 50232, 50233):
        if listener_inodes(port):
            raise RuntimeError('A required port is still listened on; preserve state and stop')
    for name, expected in source_hashes.items():
        if digest(HERE / name) != expected:
            raise RuntimeError('Recipe source changed since read-only plan')
    new.mkdir(mode=0o700)
    (new / 'bin').symlink_to(binaries, target_is_directory=True)
    save(new / 'fresh-setup-metadata.json', plan)
    subprocess.run([sys.executable, str(HERE / 'bootstrap-release-linux.py'), '--root', str(new),
                    '--archive-dir', str(archives), '--verify-existing'], check=True)
    with (new / 'funding-output.log').open('xb', buffering=0) as output:
        result = subprocess.run([sys.executable, str(HERE / 'fund-release-regtest.py'), '--root', str(new)],
                                stdout=output, stderr=subprocess.STDOUT)
    save(evidence / 'funding-script-result.json', {'time': time.time(), 'exit_code': result.returncode,
         'automatic_retry': False, 'log': str(new / 'funding-output.log')})
    result.check_returncode()
    ready = json.loads((new / 'regtest' / 'ready.json').read_text())
    if ready['wallet_broadcast'] is not False or set(ready['trusted_wallet_files']) != {'sender', 'receiver'}:
        raise RuntimeError('Fresh readiness metadata incomplete; do not start business work')
    save(evidence / 'completed.json', {'time': time.time(), 'fresh_funding_completed': True,
         'old_root_retained': str(old), 'new_ready_file': str(new / 'regtest' / 'ready.json'),
         'actual_registration_indices': ready['registration_indices'],
         'wallet_broadcast': False, 'old_claims_untouched': True,
         'independent_created_pczt_checks_required_before_prove_sign': True})
    print('New disposable environment ready; validate both genuine created PCZTs before proving/signing.')


if __name__ == '__main__':
    main()
