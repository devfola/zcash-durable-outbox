"""Workspace-only Windows QEMU guest: archives are extracted, never installed.

Run fetch, prepare, start, then ssh. Actual tested settings: WHPX,
pc,kernel-irqchip=off, qemu64, four virtual CPUs, 6144 MiB RAM.
The helper never enables Windows features or installs services/drivers.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
DOWNLOADS = json.loads(HERE.joinpath('vm-downloads.json').read_text())
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, default=Path('work/stack/vm'))
parser.add_argument('--accel', choices=['whpx', 'tcg'], default='whpx')
parser.add_argument('operation', choices=['fetch', 'prepare', 'start', 'status', 'ssh', 'tunnel', 'stop'])
args = parser.parse_args()
root = args.root.resolve()
if platform.system() != 'Windows':
    parser.error('Windows host helper; Linux hosts can run bootstrap-linux.sh directly')
root.mkdir(parents=True, exist_ok=True)


def run(command):
    return subprocess.run([str(x) for x in command], check=True)


def save(name, value):
    root.joinpath(name).write_text(json.dumps(value, indent=2) + '\n')


def verified_file(name):
    path = root / name
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, DOWNLOADS[name]['algorithm']).hexdigest()
    if digest != DOWNLOADS[name]['digest']:
        raise RuntimeError(f'Checksum mismatch: {path}')
    return path


def ssh_args():
    return ['C:/Windows/System32/OpenSSH/ssh.exe', '-i', str(root / 'guest_ed25519'), '-p', '27222',
            '-o', f'UserKnownHostsFile={root / "known_hosts"}', '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes']


def qmp(command):
    with socket.create_connection(('127.0.0.1', 27223), timeout=5) as connection:
        stream = connection.makefile('rwb', buffering=0)
        result = [json.loads(stream.readline())]
        for action in ['qmp_capabilities', command]:
            stream.write(json.dumps({'execute': action}).encode() + b'\n')
            while True:
                item = json.loads(stream.readline())
                result.append(item)
                if 'return' in item or 'error' in item:
                    break
        return result


if args.operation == 'fetch':
    for name, metadata in DOWNLOADS.items():
        path = root / name
        if not path.exists():
            # Native curl uses Windows' trusted certificate store. No TLS bypass.
            run(['C:/Windows/System32/curl.exe', '--fail', '--location', '--retry', '2',
                 '--output', path, metadata['url']])
        verified_file(name)
        print(f'Verified {name}')
    save('verified-downloads.json', DOWNLOADS)
elif args.operation == 'prepare':
    for name in DOWNLOADS:
        verified_file(name)
    if not (root / '7zip/7z.exe').exists():
        run([root / '7zr.exe', 'x', root / '7z2604-x64.exe', '-o' + str(root / '7zip'), '-y'])
    if not (root / 'qemu/qemu-system-x86_64.exe').exists():
        run([root / '7zip/7z.exe', 'x', root / 'qemu-w64-setup-20260811.exe', '-o' + str(root / 'qemu'), '-y'])
    library = root / 'pylibs'
    with zipfile.ZipFile(root / 'pycdlib-1.14.0.whl') as wheel:
        for item in wheel.namelist():
            destination = (library / item).resolve()
            if not destination.is_relative_to(library.resolve()):
                raise RuntimeError('Unsafe archive path')
        wheel.extractall(library)
    sys.path.insert(0, str(library))
    import pycdlib
    key = root / 'guest_ed25519'
    if not key.exists():
        run(['C:/Windows/System32/OpenSSH/ssh-keygen.exe', '-t', 'ed25519', '-N', '', '-f', key, '-C', 'disposable-outbox-vm'])
    seed = root / 'seed.iso'
    if not seed.exists():
        public = root.joinpath('guest_ed25519.pub').read_text().strip()
        user = f'#cloud-config\nusers:\n  - name: outbox\n    groups: [sudo]\n    sudo: ALL=(ALL) NOPASSWD:ALL\n    shell: /bin/bash\n    ssh_authorized_keys:\n      - {public}\nssh_pwauth: false\ndisable_root: true\npackage_update: false\npackage_upgrade: false\n'
        meta = f'instance-id: outbox-{uuid.uuid4()}\nlocal-hostname: outbox-qualification\n'
        image = pycdlib.PyCdlib()
        image.new(interchange_level=3, joliet=3, rock_ridge='1.09', vol_ident='cidata')
        for name, short, value in [('user-data', '/USERDATA.;1', user), ('meta-data', '/METADATA.;1', meta)]:
            raw = value.encode()
            image.add_fp(io.BytesIO(raw), len(raw), iso_path=short, rr_name=name, joliet_path='/' + name)
        image.write(str(seed))
        image.close()
    if not (root / 'guest.qcow2').exists():
        run([root / 'qemu/qemu-img.exe', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b',
             root / 'ubuntu-24.04-minimal-20261017.img', root / 'guest.qcow2', '48G'])
    run([root / 'qemu/qemu-system-x86_64.exe', '--version'])
elif args.operation == 'start':
    try:
        live = qmp('query-status')
    except OSError:
        live = None
    if live:
        raise RuntimeError('QMP port already active; inspect status instead of starting another VM')
    label = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    command = [root / 'qemu/qemu-system-x86_64.exe', '-name', 'disposable-outbox-qualification',
               '-machine', 'pc,kernel-irqchip=off' if args.accel == 'whpx' else 'pc', '-accel', args.accel,
               '-cpu', 'qemu64' if args.accel == 'whpx' else 'max', '-m', '6144', '-smp', '4', '-L', root / 'qemu',
               '-drive', f'file={root / "guest.qcow2"},if=virtio,format=qcow2',
               '-drive', f'file={root / "seed.iso"},media=cdrom,readonly=on',
               '-nic', 'user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:27222-:22',
               '-qmp', 'tcp:127.0.0.1:27223,server=on,wait=off', '-display', 'none',
               '-serial', f'file:{root / (label + "-serial.log")}', '-monitor', 'none', '-no-reboot']
    command = [str(x) for x in command]
    with root.joinpath(label + '-stdout.log').open('wb') as stdout, root.joinpath(label + '-stderr.log').open('wb') as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, cwd=root, creationflags=subprocess.CREATE_NO_WINDOW)
    save('running-vm.json', {'pid': process.pid, 'started_utc': label, 'command': command})
    print(f'VM PID {process.pid}; wait for SSH at 127.0.0.1:27222')
elif args.operation == 'status':
    print(json.dumps(qmp('query-status'), indent=2))
elif args.operation == 'ssh':
    run(ssh_args() + ['outbox@127.0.0.1'])
elif args.operation == 'tunnel':
    command = ssh_args() + ['-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15']
    for port in [29232, 50232, 50233]:
        command += ['-L', f'127.0.0.1:{port}:127.0.0.1:{port}']
    command += ['outbox@127.0.0.1']
    with root.joinpath('ssh-tunnel.log').open('ab') as log:
        child = subprocess.Popen(command, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    save('ssh-tunnel.json', {'pid': child.pid, 'command': command})
    print(f'SSH tunnel PID {child.pid}')
elif args.operation == 'stop':
    # Graceful guest shutdown; never kill unrelated host processes.
    run(ssh_args() + ['outbox@127.0.0.1', 'sudo shutdown -h now'])
