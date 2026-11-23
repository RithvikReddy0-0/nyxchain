"""Command-line interface: run nodes, create a local testnet, and use the wallet.

  python -m nyx testnet --validators 4 --out devnet
  python -m nyx node --config devnet/node1.json
  python -m nyx wallet new --out alice.json
  python -m nyx wallet show --key alice.json
  python -m nyx balance <address>
  python -m nyx send --key alice.json --to <address> --amount 50
  python -m nyx stake --key bob.json --amount 200
  python -m nyx unstake --key bob.json --amount 200
  python -m nyx status
  python -m nyx blocks
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from .crypto import KeyPair
from .tx import Transaction

DEFAULT_NODE = os.environ.get("NYX_NODE", "http://127.0.0.1:8001")


# --- HTTP helpers --------------------------------------------------------------
def api(node: str, path: str, body=None, timeout: float = 5):
    req = urllib.request.Request(
        node.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"error: {json.load(e).get('error', e)}")
    except urllib.error.URLError as e:
        raise SystemExit(f"error: cannot reach node at {node} ({e.reason})")


def send_tx(node: str, kp: KeyPair, kind: str, to: str, amount: int, fee: int) -> str:
    nonce = api(node, f"/account/{kp.address}")["next_nonce"]
    tx = Transaction(kind, kp.public_hex, to, amount, fee, nonce).sign(kp)
    return api(node, "/tx", tx.to_dict())["hash"]


# --- testnet generation ----------------------------------------------------------
def make_testnet(n: int, out: str, start_in: float = 5.0, slot_seconds: float = 2.0, stake: int = 500,
                 extra_accounts: int = 0, byzantine: list = (), attack_after: int = 6, base_port: int = 0):
    os.makedirs(os.path.join(out, "keys"), exist_ok=True)
    vals = []
    for i in range(1, n + 1):
        kp = KeyPair.generate()
        kp.save(os.path.join(out, "keys", f"validator{i}.json"))
        vals.append(kp)
    users = {}
    for name in ["alice", "bob"] + [f"user{i}" for i in range(1, extra_accounts + 1)]:
        kp = KeyPair.generate()
        kp.save(os.path.join(out, "keys", f"{name}.json"))
        users[name] = kp
    balances = {v.address: 1_000 for v in vals}
    balances[users["alice"].address] = 10_000
    balances[users["bob"].address] = 1_000
    for name, kp in users.items():
        balances.setdefault(kp.address, 100_000)
    genesis = {
        "chain_id": "nyx-devnet",
        "genesis_time": round(time.time() + start_in, 3),
        "slot_seconds": slot_seconds,
        "balances": balances,
        "stakes": {v.address: stake for v in vals},
    }
    with open(os.path.join(out, "genesis.json"), "w") as f:
        json.dump(genesis, f, indent=2)
    for i in range(1, n + 1):
        cfg = {
            "node_id": f"node{i}",
            "host": "127.0.0.1",
            "p2p_port": 7000 + base_port + i,
            "api_port": 8000 + base_port + i,
            # Only one seed each: the rest are discovered through peer exchange.
            "peers": [f"127.0.0.1:{7000 + base_port + (1 if i != 1 else 2)}"],
            "genesis_file": "genesis.json",
            "key_file": f"keys/validator{i}.json",
            "data_dir": f"data/node{i}",
        }
        if i in byzantine:
            cfg["byzantine"] = {"after_height": attack_after}
        with open(os.path.join(out, f"node{i}.json"), "w") as f:
            json.dump(cfg, f, indent=2)
    return genesis, vals, users


# --- commands --------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(prog="nyx", description="NyxChain node and wallet")
    p.add_argument("--node", default=DEFAULT_NODE, help="node API URL (default %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("node", help="run a node")
    s.add_argument("--config", required=True)

    s = sub.add_parser("testnet", help="generate keys, genesis and configs for a local network")
    s.add_argument("--validators", type=int, default=4)
    s.add_argument("--out", default="devnet")
    s.add_argument("--start-in", type=float, default=5.0, help="seconds until genesis")
    s.add_argument("--slot-seconds", type=float, default=2.0)
    s.add_argument("--stake", type=int, default=500)

    w = sub.add_parser("wallet", help="key management")
    wsub = w.add_subparsers(dest="wcmd", required=True)
    s = wsub.add_parser("new")
    s.add_argument("--out", required=True)
    s = wsub.add_parser("show")
    s.add_argument("--key", required=True)

    s = sub.add_parser("balance")
    s.add_argument("address")

    for name in ("send", "stake", "unstake"):
        s = sub.add_parser(name)
        s.add_argument("--key", required=True)
        s.add_argument("--amount", type=int, required=True)
        s.add_argument("--fee", type=int, default=1)
        if name == "send":
            s.add_argument("--to", required=True)

    sub.add_parser("status")
    s = sub.add_parser("blocks")
    s.add_argument("--from", dest="start", type=int)

    a = p.parse_args(argv)

    if a.cmd == "node":
        from .node import run_node
        run_node(a.config)
    elif a.cmd == "testnet":
        g, vals, users = make_testnet(a.validators, a.out, a.start_in, a.slot_seconds, a.stake)
        print(f"testnet written to {a.out}/ (genesis in {a.start_in:.0f}s)")
        for i, v in enumerate(vals, 1):
            print(f"  node{i}: validator {v.address}  p2p :{7000 + i}  dashboard http://127.0.0.1:{8000 + i}/")
        for n, kp in users.items():
            print(f"  {n}: {kp.address}")
    elif a.cmd == "wallet":
        if a.wcmd == "new":
            if os.path.exists(a.out):
                raise SystemExit(f"{a.out} already exists")
            kp = KeyPair.generate()
            kp.save(a.out)
            print(kp.address)
        else:
            kp = KeyPair.load(a.key)
            print(json.dumps({"address": kp.address, "public_key": kp.public_hex}, indent=2))
    elif a.cmd == "balance":
        print(json.dumps(api(a.node, f"/account/{a.address}"), indent=2))
    elif a.cmd in ("send", "stake", "unstake"):
        kp = KeyPair.load(a.key)
        kind = "transfer" if a.cmd == "send" else a.cmd
        h = send_tx(a.node, kp, kind, getattr(a, "to", ""), a.amount, a.fee)
        print(f"submitted {kind} {a.amount} NYX  tx {h}")
    elif a.cmd == "status":
        print(json.dumps(api(a.node, "/status"), indent=2))
    elif a.cmd == "blocks":
        q = f"?from={a.start}" if a.start is not None else ""
        for b in api(a.node, f"/blocks{q}"):
            fin = "final" if b["finalized"] else "     "
            print(f"#{b['height']:<5} slot {b['slot']:<6} {b['hash'][:12]}  {fin}  by {b['proposer_address'][:12]}"
                  f"  txs={b['tx_count']} votes={b['vote_count']}")


if __name__ == "__main__":
    sys.exit(main())
