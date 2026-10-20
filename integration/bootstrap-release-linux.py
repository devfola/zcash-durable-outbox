"""Verify official Linux release archives; unpack or verify explicitly reused binaries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import urllib.request


HERE = Path(__file__).resolve().parent


def file_sha256(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def archive_contents(name, archive, pin):
    digest = file_sha256(archive)
    if digest != pin['archive_sha256']:
        raise RuntimeError(f'Archive checksum mismatch: {archive}')
    allowed = {'zallet', 'zallet-zaino', 'zallet-zebra'} if name == 'zallet' else {'zebrad'}
    members = {}
    with tarfile.open(archive, 'r:gz') as package:
        for member in package.getmembers():
            basename = Path(member.name).name
            if basename in allowed and member.isfile():
                if basename in members:
                    raise RuntimeError('Duplicate release binary in archive')
                with package.extractfile(member) as source:
                    members[basename] = hashlib.file_digest(source, 'sha256').hexdigest()
    if set(members) != allowed:
        raise RuntimeError('Release archive is missing expected binaries')
    return digest, members


def verify_existing_release(binary_dir, archive_dir, pins):
    """Read-only comparison with payload hashes inside checksum-pinned archives."""
    binaries, archives = {}, {}
    for name in ['zallet', 'zebra']:
        pin = pins[name]
        digest, expected = archive_contents(name, Path(archive_dir) / pin['archive'], pin)
        archives[pin['archive']] = digest
        for basename, expected_digest in expected.items():
            actual = file_sha256(Path(binary_dir) / basename)
            if actual != expected_digest:
                raise RuntimeError(f'Existing binary differs from pinned release payload: {basename}')
            binaries[basename] = actual
    return {'pins': pins, 'binary_sha256': binaries, 'archive_sha256': archives,
            'reused_verified_existing': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('work/stack'))
    parser.add_argument('--archive-dir', type=Path, help='Existing official archives; default ROOT/archives')
    parser.add_argument('--verify-existing', action='store_true', help='Verify ROOT/bin against archives without unpacking or downloading')
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() not in ['x86_64', 'amd64']:
        parser.error('Requires x86_64 Linux; portable_vm.py prepares the Windows-hosted guest')
    os.umask(0o077)
    root = args.root.resolve()
    archive_dir = args.archive_dir.resolve() if args.archive_dir else root / 'archives'
    binary_dir = root / 'bin'
    pins = json.loads((HERE / 'pinned-sources.json').read_text())
    if args.verify_existing:
        manifest = verify_existing_release(binary_dir, archive_dir, pins)
    else:
        archive_dir.mkdir(parents=True, exist_ok=True)
        binary_dir.mkdir(parents=True, exist_ok=True)
        for name in ['zallet', 'zebra']:
            pin = pins[name]
            archive = archive_dir / pin['archive']
            repository = 'zcash/zallet' if name == 'zallet' else 'ZcashFoundation/zebra'
            url = f'https://github.com/{repository}/releases/download/{pin["tag"]}/{pin["archive"]}'
            if not archive.exists():
                with urllib.request.urlopen(url, timeout=60) as response, archive.open('xb') as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            _, expected = archive_contents(name, archive, pin)
            with tarfile.open(archive, 'r:gz') as package:
                for member in package.getmembers():
                    basename = Path(member.name).name
                    if basename in expected and member.isfile():
                        with package.extractfile(member) as source:
                            data = source.read()
                        if hashlib.sha256(data).hexdigest() != expected[basename]:
                            raise RuntimeError('Archive payload changed while reading')
                        target = binary_dir / basename
                        target.write_bytes(data)
                        target.chmod(0o700)
        manifest = verify_existing_release(binary_dir, archive_dir, pins)
        manifest['reused_verified_existing'] = False
    root.mkdir(parents=True, exist_ok=True)
    for name in ['zebrad', 'zallet-zaino']:
        subprocess.run([str(binary_dir / name), '--version'], check=True)
    target = root / 'verified-release-binaries.json'
    with target.open('x', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    print(str(binary_dir))


if __name__ == '__main__':
    main()
