"""One-shot disposable release-stack funding. Never run again in an existing state dir."""
import base64
import argparse
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import tomllib
import urllib.request
from bootstrap_registration import registration_from_accounts

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, default=Path('/home/outbox'))
ROOT = parser.parse_args().root.resolve()
STATE = ROOT / 'regtest'
BIN = ROOT / 'bin'
USER = 'disposable-regtest'
PASSWORD = 'disposable-regtest-only'
counter = 0


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w') as handle:
        json.dump(value, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def rpc(wallet, method, params=None):
    global counter
    counter += 1
    if method == 'z_gettotalbalance' and params is None:
        params = [1, True]
    call = {'jsonrpc': '2.0', 'id': counter, 'method': method, 'params': params or []}
    target = STATE / 'evidence' / f'{counter:04d}-{wallet}-{method}.json'
    record = {'started': time.time(), 'request': call, 'upstream': wallet, 'outcome': 'in-flight'}
    save(target, record)
    port = {'node': 29232, 'sender': 50232, 'receiver': 50233}[wallet]
    req = urllib.request.Request(f'http://127.0.0.1:{port}', json.dumps(call).encode(), {'Content-Type': 'application/json'})
    if wallet != 'node':
        req.add_header('Authorization', 'Basic ' + base64.b64encode(f'{USER}:{PASSWORD}'.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=900) as response:
            result = json.load(response)
        record.update(outcome='completed', response=result, finished=time.time())
        save(target, record)
        if result.get('error'):
            raise RuntimeError(str(result['error']))
        return result['result']
    except Exception as error:
        if record['outcome'] == 'in-flight':
            record.update(outcome='uncertain', error=repr(error), finished=time.time())
            save(target, record)
        raise


def wait_read(wallet, method, params=None, condition=lambda result: True, seconds=120):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            result = rpc(wallet, method, params)
            if condition(result):
                return result
            last = result
        except Exception as error:
            last = repr(error)
        time.sleep(1)
    raise RuntimeError(f'Waiting for {wallet}.{method} failed: {last}')


def launch(name, command):
    output = (STATE / f'{name}.log').open('ab', buffering=0)
    process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
    fields = Path(f'/proc/{process.pid}/stat').read_text().rsplit(')', 1)[1].split()
    save(STATE / f'{name}-process.json', {'pid': process.pid, 'command': command, 'started': time.time(),
                                       'start_ticks': fields[19]})
    return process


def main():
    os.umask(0o077)
    if STATE.exists():
        raise RuntimeError('Existing regtest directory: refuse one-shot setup replay; inspect evidence instead')
    spec = importlib.util.spec_from_file_location('configure', Path(__file__).with_name('configure-regtest.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.configure(STATE)
    node_cfg = STATE / 'zebra.toml'
    assert tomllib.loads(node_cfg.read_text())['rpc']['enable_cookie_auth'] is False
    for wallet in ['sender', 'receiver']:
        cfg = STATE / wallet / 'zallet.toml'
        text = cfg.read_text()
        assert tomllib.loads(text)['backend'] == 'zaino'
        if wallet == 'sender':
            text = text.replace('broadcast = false', 'broadcast = true')
        cfg.write_text(text)
    launch('node', [str(BIN / 'zebrad'), '-c', str(node_cfg), 'start'])
    info = wait_read('node', 'getblockchaininfo')
    genesis = rpc('node', 'getblockhash', [0])
    if info.get('chain') != 'test' or genesis != '029f11d80ef9765602235e1bc9727e3eb6ba20839319f761fee920d63401e327':
        raise RuntimeError('Wrong chain identity: pinned regtest genesis and BIP70 test name required')
    if info.get('blocks') != 0:
        raise RuntimeError('Expected fresh node at genesis; refuse an existing RPC endpoint')
    rpc('node', 'generate', [2])
    info = rpc('node', 'getblockchaininfo')
    pins = json.loads(Path(__file__).with_name('pinned-sources.json').read_text())
    expected = {upgrade['branch_id']: upgrade['height'] for upgrade in pins['network']['upgrades']}
    observed = {branch: upgrade['activationheight'] for branch, upgrade in info['upgrades'].items()}
    if observed != expected or info.get('consensus', {}).get('chaintip') != '37a5165b':
        raise RuntimeError('Node activation schedule/active branch differs from the pinned regtest profile')
    save(STATE / 'activated-chain.json', info)
    for wallet in ['sender', 'receiver']:
        for command in ['generate-encryption-identity', 'init-wallet-encryption', 'generate-mnemonic']:
            run = subprocess.run([str(BIN / 'zallet-zaino'), '-d', str(STATE / wallet), command], capture_output=True, timeout=120)
            save(STATE / 'evidence' / f'init-{wallet}-{command}.json', {'command': command, 'exit_code': run.returncode, 'stdout': run.stdout.decode(), 'stderr': run.stderr.decode()})
            run.check_returncode()
    sender = launch('sender', [str(BIN / 'zallet-zaino'), '-d', str(STATE / 'sender'), 'start'])
    launch('receiver', [str(BIN / 'zallet-zaino'), '-d', str(STATE / 'receiver'), 'start'])
    accounts = {}
    addresses = {}
    address_metadata = {}
    for wallet in ['sender', 'receiver']:
        # An empty wallet has no account scan frontier; balance RPCs can report
        # catching-up until an account exists. Account creation has its own
        # chain-height check and does not call ensure_synced.
        wait_read(wallet, 'getwalletinfo')
        accounts[wallet] = rpc(wallet, 'z_getnewaccount', [f'disposable-{wallet}'])['account_uuid']
        save(STATE / 'accounts.json', accounts)
        address_metadata[wallet] = rpc(wallet, 'z_getaddressforaccount', [accounts[wallet], ['p2pkh', 'orchard'] if wallet == 'sender' else ['orchard']])
        addresses[wallet] = address_metadata[wallet]['address']
        save(STATE / 'addresses.json', addresses)
        save(STATE / 'address-metadata.json', address_metadata)
    receivers = rpc('sender', 'z_listunifiedreceivers', [addresses['sender']])
    taddr = receivers['p2pkh']
    rpc('node', 'generatetoaddress', [102, taddr])
    wait_read('sender', 'z_gettotalbalance', condition=lambda value: Decimal(value.get('transparent', '0')) > 0)
    # Authorized isolated initial funding only. No outbox business PCZT exists yet.
    result = rpc('sender', 'z_shieldcoinbase', [taddr, addresses['sender'], None, 1, None, 'AllowRevealedSenders'])
    save(STATE / 'funding-operation.json', result)
    opid = result['opid']
    status = wait_read('sender', 'z_getoperationstatus', [[opid]], condition=lambda items: bool(items) and items[0]['status'] in ['success', 'failed'], seconds=300)
    save(STATE / 'funding-status.json', status)
    if status[0]['status'] != 'success':
        raise RuntimeError(f'Funding operation failed: {status}')
    if status[0]['result'].get('broadcast') is not True:
        raise RuntimeError('Funding operation did not report publication')
    rpc('node', 'generatetoaddress', [3, taddr])
    balances = wait_read('sender', 'z_gettotalbalance', condition=lambda value: Decimal(value.get('private', '0')) > 0)
    notes = rpc('sender', 'z_listunspent')
    save(STATE / 'funded-balances.json', balances)
    save(STATE / 'funded-notes.json', notes)
    rpc('sender', 'stop')
    sender.wait(timeout=60)
    cfg = STATE / 'sender' / 'zallet.toml'
    text = cfg.read_text()
    if 'broadcast = true' not in text:
        raise RuntimeError('Expected funding-only broadcast config')
    cfg.write_text(text.replace('broadcast = true', 'broadcast = false'))
    save(STATE / 'funding-only-broadcast-disabled.json', {'time': time.time(), 'sender': False, 'receiver': False, 'reason': 'Initial shielding complete; no outbox artifacts constructed'})
    # Keep the canonical process record pointed at the active, nonpublishing sender.
    sender = launch('sender', [str(BIN / 'zallet-zaino'), '-d', str(STATE / 'sender'), 'start'])
    wait_read('sender', 'z_gettotalbalance', condition=lambda value: Decimal(value.get('private', '0')) > 0)
    registrations = {}
    registration_indices = {}
    for wallet in ['sender', 'receiver']:
        listed = rpc(wallet, 'z_listaccounts', [True])
        viewing_key = rpc(wallet, 'z_exportviewingkey', [addresses[wallet], False])
        registration = registration_from_accounts('disposable-' + wallet, accounts[wallet], addresses[wallet],
            listed, viewing_key, address_metadata[wallet]['diversifier_index'])
        target = STATE / f'{wallet}-viewing-registration.json'
        save(target, registration)
        registrations[wallet] = str(target)
        registration_indices[wallet] = registration['registration_index']
    result = {'network': 'regtest', 'source_commit': '987382f67e622915228686e9f956c6a9c9a7514c', 'backend': 'zaino', 'accounts': accounts, 'addresses': addresses, 'registration_indices': registration_indices, 'trusted_wallet_files': registrations, 'sender_port': 50232, 'receiver_port': 50233, 'node_port': 29232, 'wallet_broadcast': False, 'config': str(STATE), 'credentials_file': str(STATE / 'credentials.json'), 'rpc_credential_file': str(STATE / 'rpc-credential.txt')}
    save(STATE / 'credentials.json', {'username': USER, 'password': PASSWORD})
    with (STATE / 'rpc-credential.txt').open('x', encoding='ascii') as handle:
        handle.write(USER + ':' + PASSWORD + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    save(STATE / 'ready.json', result)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()
