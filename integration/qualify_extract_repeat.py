"""Explicit precommit repeat-extraction experiment; never a recovery command.

Keep artifacts in private work storage. The original construction lock stays
owned. A separate durable in-flight marker serializes repeat experiments and
is retained after any uncertainty; no timeout/lease takeover is permitted.
This command never creates, proves, signs, commits an outbox record or submits.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import time

from qualify_pczt import Rpc, qualified_chain, save, save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-dir', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--wallet-lock', type=Path, required=True)
    parser.add_argument('--expected-owner', required=True)
    parser.add_argument('--credential-file', type=Path, required=True)
    parser.add_argument('--wallet-url', default='http://127.0.0.1:50232')
    parser.add_argument('--node-url', default='http://127.0.0.1:29232')
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--confirm-nonpublishing-precommit', action='store_true',
                        help='Operator confirms wallet broadcast=false and no outbox final-byte commitment for this artifact')
    args = parser.parse_args()
    if not args.confirm_nonpublishing_precommit:
        parser.error('Explicit nonpublishing/precommit qualification confirmation required; this is not recovery')
    if not 1 <= args.timeout <= 3600:
        parser.error('timeout must be 1..3600 seconds')
    original = args.original_dir.resolve()
    lock = json.loads(args.wallet_lock.read_text())
    if Path(lock['experiment']).resolve() != original or lock['owner'] != args.expected_owner:
        raise RuntimeError('Original construction ownership does not match; never steal a claim')
    summary = json.loads((original / 'result.json').read_text())
    if summary.get('submitted') is not False or summary.get('construction_lock_retained') is not True:
        raise RuntimeError('Original evidence does not describe an unsubmitted retained-lock experiment')
    signed = (original / 'signed.pczt').read_bytes()
    original_raw = (original / 'final-transaction.bin').read_bytes()
    original_sha = hashlib.sha256(original_raw).hexdigest()
    if original_sha != summary['raw_sha256']:
        raise RuntimeError('Original raw artifact integrity mismatch')
    wallet = Rpc(args.wallet_url, args.credential_file.read_text(), args.timeout)
    node = Rpc(args.node_url, None, args.timeout)
    chain, genesis = qualified_chain(node)
    target = args.evidence_dir.resolve()
    target.mkdir(parents=True, mode=0o700, exist_ok=False)
    marker = args.wallet_lock.with_name(args.wallet_lock.name + '.repeat-inflight')
    save_json(marker, {'owner': args.expected_owner, 'experiment': str(target),
                       'method': 'pczt_extract', 'pczt_sha256': hashlib.sha256(signed).hexdigest(),
                       'started_unix': time.time(), 'policy': 'retain after any uncertainty; no lease takeover'})
    save_json(target / 'node-before.json', chain)
    save_json(target / 'genesis.json', genesis)
    txid = summary['wallet_reported_txid']
    try:
        before_wallet = wallet.call('z_viewtransaction', [txid])
        save_json(target / 'wallet-before.json', before_wallet)
        if before_wallet.get('status') == 'mined' or before_wallet.get('confirmations', -1) > 0:
            raise RuntimeError('Transaction is already mined; repeat-extraction qualification is prepublication only')
        save_json(target / 'unspent-before.json', wallet.call('z_listunspent', [0, 9999999, True]))
        before_mempool = node.call('getrawmempool', [])
        save_json(target / 'mempool-before.json', before_mempool)
        if txid in before_mempool:
            raise RuntimeError('Transaction is already in the node mempool; refuse re-extraction')
        save_json(target / 'extract.started.json', {
            'method': 'pczt_extract', 'pczt_sha256': hashlib.sha256(signed).hexdigest(),
            'explicit_qualification_repeat_before_any_outbox_commit': True,
            'wallet_publication_confirmed_disabled_by_operator': True,
            'time_unix': time.time()})
        result = wallet.call('pczt_extract', [base64.b64encode(signed).decode()])
        save_json(target / 'extract.completed.json', result)
        raw = bytes.fromhex(result['hex'])
        save(target / 'final-transaction.bin', raw)
        save_json(target / 'wallet-after.json', wallet.call('z_viewtransaction', [txid]))
        save_json(target / 'unspent-after.json', wallet.call('z_listunspent', [0, 9999999, True]))
        save_json(target / 'mempool-after.json', node.call('getrawmempool', []))
        observation = {
            'same_wallet_reported_txid': result['txid'] == txid,
            'raw_bytes_changed': raw != original_raw,
            'repeated_raw_sha256': hashlib.sha256(raw).hexdigest(),
            'original_raw_sha256': original_sha, 'stored': result['stored'],
            'publication_performed': False, 'outbox_final_commit_performed': False,
            'construction_lock_retained': True, 'automatic_recovery_qualified': False,
            'single_extraction_call': True}
        save_json(target / 'result.json', observation)
        if result['txid'] != txid or result['stored'] is not True:
            raise RuntimeError('Unexpected extraction identity/storage result; reconcile and retain marker')
        # Only this confirmed-complete experimental operation is released. The
        # original wallet construction claim remains present and unchanged.
        marker.unlink()
        print('Real repeat-extraction observation saved; no publication or recovery authorization.')
    except Exception as error:
        save_json(target / 'quarantined.json', {'reason_type': type(error).__name__,
                   'policy': 'retain original ownership and repeat-inflight marker; no automatic replay'})
        raise


if __name__ == '__main__':
    main()
