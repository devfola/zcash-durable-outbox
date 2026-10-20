"""Bind a trusted address to its actual wallet-listed diversifier index."""
import uuid


def registration_from_accounts(wallet, account, address, listed_accounts, viewing_key, derived_index=None):
    account = str(uuid.UUID(account))
    accounts = [item for item in listed_accounts if item.get('account_uuid') == account]
    if len(accounts) != 1:
        raise ValueError('Expected exactly one matching account in actual z_listaccounts(true) response')
    matches = [item for item in accounts[0].get('addresses', []) if item.get('ua') == address]
    if len(matches) != 1:
        raise ValueError('Expected exactly one matching registered address under the trusted account')
    index = matches[0].get('diversifier_index')
    if type(index) is not int or not 0 <= index <= (1 << 64) - 1:
        raise ValueError('Actual registration index is absent or outside this verifier profile')
    if derived_index is not None and index != derived_index:
        raise ValueError('Address creation response and actual wallet listing disagree on diversifier index')
    if not isinstance(viewing_key, str) or not viewing_key.startswith('uviewregtest1'):
        raise ValueError('Expected the actual regtest unified full viewing key')
    return {'wallet': wallet, 'account': account, 'ufvk': viewing_key,
            'registration_address': address, 'registration_index': index, 'nu6_3_activation_height': 2}
