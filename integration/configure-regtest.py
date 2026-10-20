"""Write pinned release Zallet-zaino/Zebra configs into a NEW disposable directory.

Both wallet configs disable publication. Initial synthetic funding uses the
separate explicit bootstrap transition before any outbox business PCZT exists.
"""
import argparse
import json
import os
from pathlib import Path
import tomllib


def configure(state_dir: Path) -> dict:
    state_dir = state_dir.resolve()
    if state_dir.exists():
        raise FileExistsError(f"Refusing existing directory: {state_dir}")
    pins = json.loads(Path(__file__).with_name("pinned-sources.json").read_text())
    upgrades = pins["network"]["upgrades"]
    state_dir.mkdir(parents=True, mode=0o700)
    zebra_dir = state_dir / "zebra"
    zebra_dir.mkdir(mode=0o700)
    # A documented disposable testnet t-address is used only before wallet setup.
    zebra = f'''[network]
network = "Regtest"
listen_addr = "127.0.0.1:29233"

[network.testnet_parameters.activation_heights]
''' + "\n".join(f'{json.dumps(u["name"])} = {u["height"]}' for u in upgrades) + f'''

[state]
cache_dir = {json.dumps(str(zebra_dir))}
ephemeral = false

[rpc]
listen_addr = "127.0.0.1:29232"
enable_cookie_auth = false

[mining]
miner_address = "t27eWDgjFYJGVXmzrXeVjnb5J3uXDM9xH9v"
internal_miner = false
'''
    parsed_zebra = tomllib.loads(zebra)
    (state_dir / "zebra.toml").write_text(zebra, encoding="utf-8")
    for wallet, port in (("sender", 50232), ("receiver", 50233)):
        wallet_dir = state_dir / wallet
        wallet_dir.mkdir(mode=0o700)
        nuparams = [f'{u["branch_id"]}:{u["height"]}' for u in upgrades]
        config = f'''backend = "zaino"

[builder.limits]

[consensus]
network = "regtest"
regtest_nuparams = {json.dumps(nuparams)}

[database]

[external]
broadcast = false

[features]
as_of_version = "0.1.0-beta.3"

[features.deprecated]

[features.experimental]

[indexer]
validator_address = "127.0.0.1:29232"
validator_user = "disposable-regtest"
validator_password = "disposable-regtest-only"

[keystore]
require_backup = false

[note_management]

[rpc]
bind = ["127.0.0.1:{port}"]
timeout = 900

[[rpc.auth]]
user = "disposable-regtest"
password = "disposable-regtest-only"
'''
        parsed = tomllib.loads(config)
        assert parsed["external"]["broadcast"] is False
        for upgrade, nuparam in zip(upgrades, parsed["consensus"]["regtest_nuparams"]):
            branch, height = nuparam.split(":")
            assert branch == upgrade["branch_id"]
            assert int(height) == parsed_zebra["network"]["testnet_parameters"]["activation_heights"][upgrade["name"]]
        (wallet_dir / "zallet.toml").write_text(config, encoding="utf-8")
    marker = {"network": "regtest", "state_dir": str(state_dir), "wallet_broadcast": False,
              "config_status": "TOML parity checked; matching release config parsed by actual binaries in recorded VM run",
              "zebra_rpc": "http://127.0.0.1:29232", "sender_rpc": "http://127.0.0.1:50232",
              "receiver_rpc": "http://127.0.0.1:50233", "pins": pins}
    (state_dir / "disposable-regtest.json").write_text(json.dumps(marker, indent=2) + "\n")
    for file in state_dir.rglob("*"):
        if file.is_file():
            os.chmod(file, 0o600)
    return marker


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(configure(args.state_dir), indent=2))
