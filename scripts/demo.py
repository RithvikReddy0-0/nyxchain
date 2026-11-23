"""End-to-end demo: a 4-validator network with payments, staking, finality and a node crash.

  python scripts/demo.py

1. Generate a fresh testnet and start 4 validator nodes (each knows only one seed).
2. Peer discovery, block production and finality.
3. Wallet: Alice pays Bob, Bob stakes and becomes a (low-reputation) validator, then unstakes part.
4. Kill node3. The chain and finality keep going; node3's reputation decays each time it misses its slot.
5. Restart node3. It reloads its blocks from disk, syncs the rest from peers, and rejoins.
6. Reputation = recent reliability: running validators converge, an idle staker fades.
7. Whale: Alice stakes 18x a normal validator but never runs a node. The 30% cap keeps her
   share below a blocking third, so finality keeps going without her.
8. Unbonded coins are released; every node agrees on the finalized chain.
Exits non-zero if any check fails, so it doubles as an integration test.
Set NYX_SHOTS=<folder> to save dashboard screenshots at each stage.
"""
import os
import sys
import time

from common import (Network, api, check, heading, node_url, screenshot, status, summary, table,
                    validators_table, wait_until)

from nyx.cli import make_testnet, send_tx
from nyx.crypto import KeyPair

N = 4
SLOT = 1.0          # 1-second slots keep the demo short
SHOTS = os.environ.get("NYX_SHOTS", "")


def shot(name):
    if SHOTS:
        screenshot(node_url(1) + "/", os.path.join(SHOTS, name))


def main():
    net = Network("nyx-demo-")
    d = net.dir
    try:
        heading("1. Generate testnet and start 4 validators")
        genesis, vals, users = make_testnet(N, d, start_in=4, slot_seconds=SLOT, stake=500)
        alice = KeyPair.load(os.path.join(d, "keys", "alice.json"))
        bob = KeyPair.load(os.path.join(d, "keys", "bob.json"))
        for i in range(1, N + 1):
            net.start(i)
        print(f"   data and logs: {d}")
        time.sleep(12)

        heading("2. Peer discovery, block production and finality")
        table(range(1, N + 1))
        sts = [status(i) for i in range(1, N + 1)]
        check("every node found all 3 peers (from a single seed)", all(len(s["peers"]) == N - 1 for s in sts))
        check("blocks are being produced", min(s["height"] for s in sts) >= 4, f"min height {min(s['height'] for s in sts)}")
        fin = min(s["finalized_height"] for s in sts)
        check("blocks are being finalized by 2/3 votes", fin >= 2, f"finalized height {fin}, tip {sts[0]['height']}")
        shot("01_network_running.png")

        heading("3. Wallet: Alice pays Bob, Bob stakes, then unstakes part")
        for amount in (100, 150):
            send_tx(node_url(1), alice, "transfer", bob.address, amount, 1)
        print("   alice -> bob: 100 NYX, 150 NYX (submitted to node1)")
        time.sleep(3 * SLOT)
        send_tx(node_url(2), bob, "stake", "", 300, 1)
        print("   bob stakes 300 NYX (submitted to node2)")
        time.sleep(4 * SLOT)
        bals = [api(node_url(i), f"/account/{bob.address}") for i in range(1, N + 1)]
        print(f"   bob as seen by node1..4: balance {[b['balance'] for b in bals]}, stake {[b['stake'] for b in bals]}")
        check("all nodes agree on bob's account", len({(b["balance"], b["stake"]) for b in bals}) == 1)
        check("bob received 250 and staked 300", bals[0]["balance"] == 1000 + 250 - 300 - 1 and bals[0]["stake"] == 300)
        check("new validator joins at low reputation (10%, then it must earn it)", bals[0]["reputation_pct"] <= 10.0,
              f"{bals[0]['reputation_pct']:.1f}% vs 50%+ for genesis validators")
        send_tx(node_url(3), bob, "unstake", "", 100, 1)
        print("   bob unstakes 100 NYX (submitted to node3)")
        acct = wait_until(lambda: (lambda a: a if a["unbonding"] else None)(api(node_url(1), f"/account/{bob.address}")), 8)
        check("unstaked coins are locked while unbonding, not returned at once",
              acct and acct["unbonding"][0][1] == 100 and acct["stake"] == 200,
              f"unbonding {acct['unbonding'] if acct else None}, balance {acct['balance'] if acct else '?'}")
        bal_unbonding = acct["balance"] if acct else 0

        heading("4. Kill node3 (hard crash)")
        before = validators_table(1)
        v3 = vals[2].address
        net.kill(3)
        s0 = status(1)
        print("   node3 killed; waiting 20 slots...")
        time.sleep(20 * SLOT)
        alive = [1, 2, 4]
        table(range(1, N + 1))
        after = validators_table(1)
        s1 = status(1)
        check("chain kept growing without node3", s1["height"] - s0["height"] >= 8,
              f"+{s1['height'] - s0['height']} blocks in 20 slots")
        check("finality kept advancing without node3", s1["finalized_height"] - s0["finalized_height"] >= 5,
              f"finalized {s0['finalized_height']} -> {s1['finalized_height']}")
        check("node3's reputation fell", after[v3]["reputation_pct"] < before[v3]["reputation_pct"],
              f"{before[v3]['reputation_pct']:.0f}% -> {after[v3]['reputation_pct']:.0f}%")
        agreed = wait_until(lambda: len({status(i)["tip"] for i in alive}) == 1, 3, 0.2)
        check("surviving nodes agree on the tip", agreed)
        shot("02_node3_down.png")

        heading("5. Restart node3")
        net.start(3)
        caught = wait_until(lambda: (lambda hs: max(hs) - hs[2] <= 1)([status(i)["height"] for i in range(1, N + 1)]), 20)
        time.sleep(2)
        table(range(1, N + 1))
        hs = [status(i)["height"] for i in range(1, N + 1)]
        check("node3 caught up with the network", caught, f"heights {hs}")
        check("node3 restored blocks from disk", "restored" in net.log(3))
        shot("03_node3_recovered.png")

        heading("6. Reputation is recent reliability, not accumulated history")
        s = status(1)
        reps = {v["address"]: v["reputation_pct"] for v in s["validators"]}
        running = [reps[v.address] for v in vals if v.address in reps and v.address != vals[2].address]
        print(f"   running validators (node1, node2, node4): {[round(r) for r in running]}%")
        print(f"   node3 (back after crash): {reps.get(vals[2].address, 0):.0f}%   bob (staked, never ran a node): {reps.get(bob.address, 0):.0f}%")
        check("validators doing the same work have near-equal reputation", max(running) - min(running) < 6,
              f"spread {max(running) - min(running):.1f} points")
        check("a staker that never runs a node loses reputation", reps.get(bob.address, 100) < 10,
              f"bob 10% -> {reps.get(bob.address, 0):.0f}%")

        heading("7. Whale: Alice stakes 9,000 NYX (18x a normal validator) and stays offline")
        send_tx(node_url(1), alice, "stake", "", 9000, 1)
        whale = wait_until(lambda: next((v for v in status(1)["validators"] if v["address"] == alice.address), None), 8)
        s = status(1)
        raw_total = sum(v["raw_weight"] for v in s["validators"])
        raw_share = whale["raw_weight"] * 100 / raw_total if whale else 0
        print(f"   alice raw weight {whale['raw_weight'] if whale else '?'} = {raw_share:.0f}% of raw total; "
              f"capped weight {whale['weight'] if whale else '?'} = {whale['share_pct'] if whale else '?'}%")
        check("the whale's voting share is capped at 30%", whale and whale["share_pct"] <= 30.0 and whale["capped"],
              f"{raw_share:.0f}% uncapped -> {whale['share_pct'] if whale else '?'}%")
        f0 = s["finalized_height"]
        time.sleep(12 * SLOT)
        f1 = status(1)["finalized_height"]
        offline_raw = (whale["raw_weight"] if whale else 0) + sum(v["raw_weight"] for v in s["validators"] if v["address"] == bob.address)
        offline_cap = sum(v["weight"] for v in s["validators"] if v["address"] in (alice.address, bob.address))
        total_cap = sum(v["weight"] for v in s["validators"])
        off_raw_pct, off_cap_pct = offline_raw * 100 / raw_total, offline_cap * 100 / total_cap
        verdict = "above 1/3: finality would stall" if off_raw_pct > 100 / 3 else "below 1/3"
        print(f"   offline weight (alice + bob): {off_raw_pct:.0f}% without the cap ({verdict}), "
              f"{off_cap_pct:.0f}% with it")
        check("finality keeps advancing although the whale never votes", f1 - f0 >= 5, f"finalized {f0} -> {f1}")
        shot("04_whale_capped.png")

        heading("8. Unbonding release and agreement on finalized history")
        acct = api(node_url(1), f"/account/{bob.address}")
        check("unbonded coins were released after the unbonding period",
              not acct["unbonding"] and acct["balance"] >= bal_unbonding + 100, f"balance now {acct['balance']}")
        sts = [status(i) for i in range(1, N + 1)]
        f = min(s["finalized_height"] for s in sts)
        hashes = {api(node_url(i), f"/block/{f}")["hash"] for i in range(1, N + 1)}
        check("all 4 nodes have the identical finalized block", len(hashes) == 1, f"block #{f} on every node")
        rejected = sum(net.log(i).count("REJECT") for i in range(1, N + 1))
        check("no honest block was ever rejected", rejected == 0, f"{rejected} rejections in logs")
        return summary()
    finally:
        net.stop(keep_to=os.environ.get("NYX_LOGS"))


if __name__ == "__main__":
    sys.exit(main())
