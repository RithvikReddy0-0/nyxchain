"""Deterministic simulation of the reputation rule and the voting-weight cap, using the real chain code.

  python scripts/reputation_sim.py [--out folder]

Five validators run for 300 blocks under a simulated clock:
  Founder      genesis validator (starts at 50%), always online
  Newcomer     stakes at block 1 (starts at 10%), always online
  Quitter      genesis validator, goes offline for good at block 150
  Flaky        genesis validator, misses about 3 duties in 10
  Crash+return genesis validator, offline from block 80 to block 130

Shows that reputation tracks recent reliability: the newcomer catches up with the founder,
a long good history does not protect the quitter, and a returning node earns trust back.
Also draws how the 30% cap limits a whale. Writes reputation_sim.json and two PNG charts.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nyx.chain import Chain  # noqa: E402
from nyx.crypto import KeyPair, sha256_hex  # noqa: E402
from nyx.params import REP_MAX  # noqa: E402
from nyx.state import State, cap_weights  # noqa: E402
from nyx.tx import Transaction  # noqa: E402
from nyx.vote import Vote  # noqa: E402

GENESIS_TIME = 1_000_000
BLOCKS = 300


class Clock:
    ms = GENESIS_TIME * 1000

    def __call__(self):
        return self.ms


def online(name, height):
    if name == "Quitter":
        return height < 150
    if name == "Crash+return":
        return not (80 <= height < 130)
    if name == "Flaky":
        return (height * 7) % 10 >= 3        # a fixed pattern: offline 3 heights in every 10
    return True


def simulate():
    names = ["Founder", "Newcomer", "Quitter", "Flaky", "Crash+return"]
    # Fixed keys derived from the names, so every run gives exactly the same chain and chart.
    keys = {n: KeyPair.from_private_hex(sha256_hex(n.encode())) for n in names}
    genesis = {
        "chain_id": "sim", "genesis_time": GENESIS_TIME, "slot_seconds": 1,
        "balances": {k.address: 1000 for k in keys.values()},
        "stakes": {keys[n].address: 500 for n in names if n != "Newcomer"},
    }
    clock = Clock()
    chain = Chain(genesis, clock=clock)
    by_addr = {k.address: (n, k) for n, k in keys.items()}
    nk = keys["Newcomer"]
    first_txs = [Transaction("stake", nk.public_hex, "", 500, 1, 0).sign(nk)]

    series = {n: [] for n in names}
    slot, pending = 1, []
    while chain.height < BLOCKS:
        clock.ms = GENESIS_TIME * 1000 + slot * 1000 + 100
        p = chain.proposer_for(slot)
        name, kp = by_addr[p]
        if online(name, chain.height + 1):
            b = chain.build_block(kp, slot, first_txs, pending)
            chain.add_block(b)
            first_txs = []
            pending = [Vote(b.height, b.hash, keys[n].public_hex).sign(keys[n]).to_dict()
                       for n in names if online(n, b.height) and chain.state.weight(keys[n].address) > 0]
            for n in names:
                series[n].append(round(chain.state.rep(keys[n].address) * 100 / REP_MAX, 2))
        slot += 1
    return series, chain


def cap_example():
    """A whale's share of voting weight with and without the cap, as its stake grows.
    Four other validators hold 500 each; everyone is at 80% reputation."""
    out = []
    for whale_stake in list(range(100, 2000, 100)) + list(range(2000, 30001, 500)):
        st = State()
        st.stakes = {f"v{i}": 500 for i in range(4)}
        st.stakes["whale"] = whale_stake
        st.reputation = {a: 8000 for a in st.stakes}
        raw = [(a, st.raw_weight(a)) for a in sorted(st.stakes)]
        capped = cap_weights(raw)
        out.append((dict(raw)["whale"] * 100 / sum(w for _, w in raw),
                    dict(capped)["whale"] * 100 / sum(w for _, w in capped)))
    return out


def charts(series, cap, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, muted, grid = "#1f2430", "#6b7280", "#e5e7eb"
    colors = {"Founder": "#5b4bd6", "Newcomer": "#0f9d8a", "Quitter": "#d97706", "Flaky": "#9ca3af",
              "Crash+return": "#dc2626"}
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": muted,
                         "axes.labelcolor": ink, "xtick.color": muted, "ytick.color": muted,
                         "axes.spines.top": False, "axes.spines.right": False})

    fig, ax = plt.subplots(figsize=(7.4, 3.8))
    ax.axvspan(80, 130, color="#fde8e8", zorder=0)
    ax.axvline(150, color="#d97706", lw=0.8, ls=":")
    for n, ys in series.items():
        ax.plot(range(1, len(ys) + 1), ys, color=colors[n], lw=2 if n in ("Founder", "Newcomer") else 1.6)
    # End labels, nudged apart so lines that finish close together stay readable.
    ends = sorted(((ys[-1], n) for n, ys in series.items()), reverse=True)
    placed = []
    for y, n in ends:
        ly = y if not placed else min(y, placed[-1] - 6)
        placed.append(ly)
        ax.text(len(series[n]) + 4, ly, f"{n} {y:.0f}%", color=colors[n], va="center", fontsize=9)
    ax.text(105, 3, "Crash+return\noffline", color="#b91c1c", ha="center", fontsize=8)
    ax.text(152, 3, "Quitter leaves", color="#b45309", fontsize=8)
    ax.set_xlim(0, len(series["Founder"]) + 70)
    ax.set_ylim(0, 102)
    ax.set_xlabel("Block height")
    ax.set_ylabel("Reputation (%)")
    ax.set_title("Reputation tracks recent reliability: same work, same score",
                 loc="left", color=ink, fontsize=11)
    ax.grid(axis="y", color=grid)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_reputation.png"), dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.4, 3.4))
    xs = [c[0] for c in cap]
    ax.plot(xs, xs, color="#9ca3af", lw=2, ls="--", label="Without cap")
    ax.plot(xs, [c[1] for c in cap], color="#5b4bd6", lw=2.4, label="With cap")
    ax.axhline(100 / 3, color="#dc2626", lw=1)
    ax.text(2, 100 / 3 + 2, "1/3: a validator above this can stall finality alone", color="#dc2626", fontsize=8.5)
    i = next((i for i, (x, c) in enumerate(cap) if c > 30.05), None)
    if i:
        knee = (cap[i - 1][0] + cap[i][0]) / 2
        ax.axvline(knee, color=muted, lw=0.8, ls=":")
        ax.text(knee + 1, 6, f"beyond about {knee:.0f}% the cap would have to\nmore than double everyone else's share,\nso it loosens", color=muted, fontsize=8)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_xlabel("Whale's share of raw weight (stake x reputation), %")
    ax.set_ylabel("Whale's share of voting weight (%)")
    ax.legend(frameon=False, loc="upper left")
    ax.set_title("The cap holds one validator to 30% without boosting the small ones more than 2x",
                 loc="left", color=ink, fontsize=10.5)
    ax.grid(axis="y", color=grid)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_cap.png"), dpi=180)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sim_out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    series, chain = simulate()
    cap = cap_example()
    print(f"simulated {chain.height} blocks; final reputation:")
    for n, ys in series.items():
        print(f"  {n:<13} {ys[-1]:6.1f}%   (at block 150: {ys[149]:.1f}%)")
    print("whale share of voting weight (uncapped -> capped):")
    for u, c in cap[::6]:
        print(f"  {u:5.1f}% -> {c:5.1f}%")
    with open(os.path.join(a.out, "reputation_sim.json"), "w") as f:
        json.dump({"series": series, "cap": cap}, f)
    try:
        charts(series, cap, a.out)
    except ImportError:
        print("(matplotlib not installed: skipping charts)")
    print(f"written to {a.out}/")


if __name__ == "__main__":
    main()
