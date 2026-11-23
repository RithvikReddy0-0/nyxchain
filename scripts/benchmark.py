"""Evaluation: throughput, inclusion and finality latency, and crash recovery time.

  python scripts/benchmark.py [--out results_folder]

Runs a fresh 4-validator network (1-second slots) on this machine and measures:
  1. Throughput under two loads (1,000 and 4,000 signed transfers from many accounts)
  2. Time from submission to inclusion, and from block production to finality
  3. Crash recovery: chain liveness while a node is down, and its catch-up time after restart
Writes benchmark_results.json and charts (PNG, needs matplotlib) to the output folder.
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time

from common import Network, api, heading, node_url, status, wait_until

from nyx.cli import make_testnet
from nyx.crypto import KeyPair
from nyx.tx import Transaction

N = 4
SLOT = 1.0
BP = 200


def url(i):
    return node_url(i, BP)


class FinalityWatcher(threading.Thread):
    """Polls node1 and records when each height first appears and first becomes final."""

    def __init__(self):
        super().__init__(daemon=True)
        self.seen, self.final, self.stop = {}, {}, False
        self.heights = []   # (time, height, finalized)

    def run(self):
        while not self.stop:
            s = status(1, BP)
            if s:
                now = time.time()
                self.heights.append((now, s["height"], s["finalized_height"]))
                for h in range(1, s["height"] + 1):
                    self.seen.setdefault(h, now)
                for h in range(1, s["finalized_height"] + 1):
                    self.final.setdefault(h, now)
            time.sleep(0.05)


def load_test(users, per_user, label):
    heading(f"Throughput: {len(users) * per_user} transfers from {len(users)} accounts ({label})")
    sink = KeyPair.generate().address
    batches = {i: [] for i in range(1, N + 1)}
    t_sign = time.time()
    k = 0
    for kp in users:
        base = api(url(1), f"/account/{kp.address}")["next_nonce"]
        for n in range(per_user):
            tx = Transaction("transfer", kp.public_hex, sink, 1, 1, base + n).sign(kp)
            batches[1 + k % N].append(tx.to_dict())
            k += 1
    total = k
    print(f"   signed {total} transactions in {time.time() - t_sign:.2f}s")
    h_start = status(1, BP)["height"]
    t0 = time.time()
    for i, batch in batches.items():                       # spread the load over all four nodes
        for j in range(0, len(batch), 500):
            api(url(i), "/txs", batch[j:j + 500], timeout=60)
    t_sub = time.time() - t0
    print(f"   submitted to 4 nodes in {t_sub:.2f}s")

    def all_in():
        return api(url(1), f"/account/{sink}")["balance"] >= total
    wait_until(all_in, 180, 0.2)
    t_done = time.time()
    h_end = status(1, BP)["height"]
    blocks = api(url(1), f"/blocks?from={h_start + 1}&limit={h_end - h_start}")
    per_block = [(b["height"], b["tx_count"], b["timestamp_ms"] / 1000) for b in blocks if b["tx_count"]]
    first_ts = min(ts for _, _, ts in per_block)
    last_ts = max(ts for _, _, ts in per_block)
    span = max(SLOT, last_ts - first_ts + SLOT)   # the slots that held the load
    tps = total / span
    print(f"   all {total} included in {len(per_block)} blocks; span {span:.1f}s -> {tps:.0f} tx/s; "
          f"end-to-end {t_done - t0:.1f}s")
    print(f"   largest block: {max(c for _, c, _ in per_block)} txs")
    return {"label": label, "transactions": total, "accounts": len(users), "submit_seconds": round(t_sub, 2),
            "blocks_used": len(per_block), "busy_span_seconds": round(span, 2), "tps": round(tps, 1),
            "end_to_end_seconds": round(t_done - t0, 2), "per_block": per_block}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="benchmark_out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    net = Network("nyx-bench-")
    d = net.dir
    res = {"setup": {"validators": N, "slot_seconds": SLOT, "machine": "single host, all nodes local",
                     "python": sys.version.split()[0]}}
    try:
        make_testnet(N, d, start_in=4, slot_seconds=SLOT, stake=500, extra_accounts=80, base_port=BP)
        for i in range(1, N + 1):
            net.start(i)
        users = [KeyPair.load(os.path.join(d, "keys", f"user{i}.json")) for i in range(1, 81)]
        watch = FinalityWatcher()
        wait_until(lambda: status(1, BP) and status(1, BP)["finalized_height"] >= 3, 30)
        watch.start()

        res["load_small"] = load_test(users[:20], 50, "20 accounts x 50")
        time.sleep(3)
        res["load_large"] = load_test(users, 50, "80 accounts x 50")
        time.sleep(3)

        heading("Crash recovery: kill node2 for 15 s, then restart it")
        v2 = api(url(2), "/status")["validator"]
        rep_series = []
        t_kill = time.time()
        h_kill = status(1, BP)["height"]
        net.kill(2)
        while time.time() - t_kill < 15:
            s = status(1, BP)
            r = next((v["reputation_pct"] for v in s["validators"] if v["address"] == v2), 0)
            rep_series.append((round(time.time() - t_kill, 2), s["height"], s["finalized_height"], r))
            time.sleep(0.5)
        h_mid = status(1, BP)["height"]
        t_restart = time.time()
        net.start(2)
        caught = wait_until(lambda: (status(2, BP) or {}).get("height", -1) >= status(1, BP)["height"] - 1, 60, 0.1)
        t_caught = time.time()
        while time.time() - t_kill < 35:
            s = status(1, BP)
            r = next((v["reputation_pct"] for v in s["validators"] if v["address"] == v2), 0)
            rep_series.append((round(time.time() - t_kill, 2), s["height"], s["finalized_height"], r))
            time.sleep(0.5)
        res["recovery"] = {
            "down_seconds": round(t_restart - t_kill, 1),
            "blocks_while_down": h_mid - h_kill,
            "catch_up_seconds": round(t_caught - t_restart, 2) if caught else None,
            "rep_series": rep_series,
        }
        print(f"   {h_mid - h_kill} blocks produced while node2 was down; "
              f"node2 back in sync {res['recovery']['catch_up_seconds']}s after restart")
        watch.stop = True
        heading("Finality latency (block production -> finalized, whole run incl. crash)")
        lat = [watch.final[h] - watch.seen[h] for h in sorted(watch.final) if h in watch.seen and h > 3]
        res["finality"] = {
            "blocks_measured": len(lat),
            "median_seconds": round(statistics.median(lat), 2),
            "p90_seconds": round(sorted(lat)[int(len(lat) * 0.9) - 1], 2),
            "max_seconds": round(max(lat), 2),
            "samples": [round(x, 3) for x in lat],
        }
        print(f"   {len(lat)} blocks: median {res['finality']['median_seconds']}s, "
              f"p90 {res['finality']['p90_seconds']}s, max {res['finality']['max_seconds']}s")

        res["height_series"] = [(round(t - watch.heights[0][0], 2), h, f) for t, h, f in watch.heights[::4]]
    finally:
        net.stop()

    with open(os.path.join(a.out, "benchmark_results.json"), "w") as f:
        json.dump(res, f, indent=2)
    try:
        charts(res, a.out)
    except ImportError:
        print("   (matplotlib not installed: skipping charts)")
    print(f"\nresults written to {a.out}/")


def charts(res, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, muted, grid, c1, c2, c3 = "#1f2430", "#6b7280", "#e5e7eb", "#5b4bd6", "#0f9d8a", "#d97706"
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": muted,
                         "axes.labelcolor": ink, "xtick.color": muted, "ytick.color": muted,
                         "axes.spines.top": False, "axes.spines.right": False})

    # 1. Transactions per block under the large load
    pb = res["load_large"]["per_block"]
    fig, ax = plt.subplots(figsize=(7, 3.2))
    ax.bar([str(h) for h, _, _ in pb], [c for _, c, _ in pb], color=c1, width=0.6)
    ax.axhline(500, color=c3, ls="--", lw=1)
    ax.text(len(pb) - 0.5, 505, "block limit 500", color=c3, ha="right", va="bottom", fontsize=9)
    ax.set_xlabel("Block height")
    ax.set_ylabel("Transactions")
    ax.set_title(f"{res['load_large']['transactions']:,} transfers cleared in {len(pb)} blocks "
                 f"({res['load_large']['tps']:.0f} tx/s)", loc="left", color=ink, fontsize=11)
    ax.grid(axis="y", color=grid)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_throughput.png"), dpi=180)
    plt.close(fig)

    # 2. Finality latency distribution
    s = res["finality"]["samples"]
    fig, ax = plt.subplots(figsize=(7, 3.2))
    ax.hist(s, bins=20, color=c2)
    ax.axvline(res["finality"]["median_seconds"], color=ink, lw=1)
    ax.text(res["finality"]["median_seconds"], ax.get_ylim()[1] * 0.92, f"  median {res['finality']['median_seconds']} s",
            color=ink, fontsize=9)
    ax.set_xlabel("Seconds from block appearing to block finalized (1-second slots)")
    ax.set_ylabel("Blocks")
    ax.set_title(f"Finality latency over {res['finality']['blocks_measured']} blocks", loc="left", color=ink, fontsize=11)
    ax.grid(axis="y", color=grid)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_finality.png"), dpi=180)
    plt.close(fig)

    # 3. Crash recovery: chain height keeps growing, crashed validator's reputation decays then recovers
    r = res["recovery"]
    t = [x[0] for x in r["rep_series"]]
    fig, ax = plt.subplots(figsize=(7, 3.4))
    ax.plot(t, [x[1] for x in r["rep_series"]], color=c1, lw=2, label="Block height")
    ax.plot(t, [x[2] for x in r["rep_series"]], color=c2, lw=2, label="Finalized height")
    ax.axvspan(0, r["down_seconds"], color="#fde8e8", zorder=0)
    ax.text(0.3, ax.get_ylim()[1], "node2 down", color="#b91c1c", va="top", fontsize=9)
    ax.set_xlabel("Seconds since node2 crashed")
    ax.set_ylabel("Height")
    ax2 = ax.twinx()
    ax2.plot(t, [x[3] for x in r["rep_series"]], color=c3, lw=2, ls="--", label="node2 reputation")
    ax2.set_ylabel("node2 reputation (%)", color=c3)
    ax2.tick_params(axis="y", colors=c3)
    ax2.spines["right"].set_visible(True)
    ax2.set_ylim(0, 100)
    lines = ax.get_lines() + ax2.get_lines()
    ax.legend(lines, [l.get_label() for l in lines], loc="lower right", frameon=False, fontsize=9)
    ax.set_title("Chain and finality keep going while a validator is down", loc="left", color=ink, fontsize=11)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_recovery.png"), dpi=180)
    plt.close(fig)


if __name__ == "__main__":
    main()
