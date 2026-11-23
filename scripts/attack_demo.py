"""Attack demo: a Byzantine validator equivocates and is caught and slashed.

  python scripts/attack_demo.py

node4 runs in attack mode. On its first turn after block 6 it signs TWO different
blocks for the same slot, sends one to each half of the network, and votes for
both. Honest nodes see both signed blocks, build the proof (evidence), and the
next honest proposer puts it on chain: node4 loses half its stake, its
reputation goes to zero, and it is removed from the validator set.
"""
import os
import sys
import time

from common import Network, api, check, heading, node_url, screenshot, status, summary, table, validators_table, wait_until

from nyx.cli import make_testnet

N = 4
SLOT = 1.0
BP = 100          # ports 7101.. / 8101.. so this can run beside the normal demo
SHOTS = os.environ.get("NYX_SHOTS", "")


def main():
    net = Network("nyx-attack-")
    d = net.dir
    try:
        heading("1. Start 3 honest validators and 1 Byzantine validator (node4)")
        genesis, vals, users = make_testnet(N, d, start_in=4, slot_seconds=SLOT, stake=500,
                                            byzantine=[4], attack_after=6, base_port=BP)
        cheat = vals[3].address
        for i in range(1, N + 1):
            net.start(i)
        print(f"   node4 = {cheat}  (attack mode)")
        time.sleep(8)
        table(range(1, N + 1), BP)
        before = validators_table(1, BP)

        heading("2. Wait for node4's turn: it signs two blocks for one slot")
        slashed = wait_until(lambda: status(1, BP)["slashed"], 120)
        attack_line = next((l for l in net.log(4).splitlines() if "ATTACK" in l), "")
        print("   node4 log: " + attack_line.split("ATTACK", 1)[-1].strip()[:150])
        check("node4 equivocated (two signed blocks for one slot, two votes)", bool(attack_line))
        caught = [i for i in (1, 2, 3) if "CAUGHT" in net.log(i)]
        for i in caught[:3]:
            line = next(l for l in net.log(i).splitlines() if "CAUGHT" in l)
            print(f"   node{i} log: " + line.split("CAUGHT", 1)[-1].strip())
        check("honest nodes detected the equivocation", len(caught) >= 2, f"detected by nodes {caught}")

        heading("3. Evidence on chain: node4 is slashed")
        time.sleep(3)
        s = status(1, BP)
        after = validators_table(1, BP)
        sl = {x["address"]: x for x in s["slashed"]}
        check("node4 is slashed on chain", cheat in sl, f"at block #{sl[cheat]['height']}" if cheat in sl else "")
        check("node4 lost half its stake", cheat in sl and sl[cheat]["stake"] == before[cheat]["stake"] // 2,
              f"{before[cheat]['stake']} -> {sl[cheat]['stake'] if cheat in sl else '?'} NYX")
        check("node4 was removed from the validator set", cheat not in after)
        check("slashed coins were burned (minus a 10% reward to the reporter)", s["burned"] == 225, f"burned {s['burned']} NYX")
        blk = api(node_url(1, BP), f"/block/{sl[cheat]['height']}") if cheat in sl else {}
        ev = blk.get("evidence", [])
        print(f"   evidence in block #{blk.get('height')}: {[e['kind'] for e in ev]}")
        check("the block carries the signed proof", len(ev) >= 1)
        if SHOTS:
            screenshot(node_url(1, BP) + "/", os.path.join(SHOTS, "04_byzantine_slashed.png"))

        heading("4. Network after the attack")
        f0 = status(1, BP)["finalized_height"]
        time.sleep(10 * SLOT)
        table(range(1, N + 1), BP)
        sts = [status(i, BP) for i in (1, 2, 3)]
        check("honest chain and finality keep going", min(x["finalized_height"] for x in sts) > f0,
              f"finalized {f0} -> {min(x['finalized_height'] for x in sts)}")
        f = min(x["finalized_height"] for x in sts)
        hashes = {api(node_url(i, BP), f"/block/{f}")["hash"] for i in (1, 2, 3, 4)}
        check("no conflicting finalized blocks: all nodes share block #%d" % f, len(hashes) == 1)
        kinds = sorted({k for i in (1, 2, 3) for k in ("double vote", "two blocks") if k in net.log(i)})
        print(f"   kinds of equivocation detected across honest nodes: {kinds}")
        return summary()
    finally:
        net.stop(keep_to=os.environ.get("NYX_LOGS_ATTACK"))


if __name__ == "__main__":
    sys.exit(main())
