# Proof index

Every claim in the report maps to a file here. All files were produced by the scripts in `NyxChain/` on 23 November 2026 (4 validators, 1-second slots, one machine, 2 CPU cores, Python 3.13).

| Claim | Where to see it |
|---|---|
| 30 unit tests pass (crypto, state, blocks, reputation, self-healing, cap, fork choice, finality, slashing, mempool) | `01_unit_tests.txt` |
| Nodes discover each other from one seed address | `02_demo_output.txt` stage 2 |
| Blocks are finalized by ⅔ of voting weight | `02_demo_output.txt` stage 2; `FINAL` lines in `logs/demo/*.log` |
| Wallet transfers and staking agree on every node | `02_demo_output.txt` stage 3 |
| Late stakers join at 10 % reputation | `02_demo_output.txt` stage 3 |
| Unstaked coins are locked for 10 blocks, then released | `02_demo_output.txt` stages 3 and 8 |
| Chain and finality keep going while a node is crashed | `02_demo_output.txt` stage 4; `screenshots/02_node3_down.png`; `benchmark/fig_recovery.png` |
| A crashed validator's reliability falls automatically | `MISSED` lines in `logs/demo/node1.log`; `02_demo_output.txt` stage 4 |
| A restarted node reloads from disk and catches up | `02_demo_output.txt` stage 5; `DISK restored` line in `logs/demo/node3.log`; `screenshots/03_node3_recovered.png` |
| Same work gives the same reputation; an idle staker fades | `02_demo_output.txt` stage 6; `reputation_sim/fig_reputation.png`; `05_reputation_sim_output.txt` |
| Old work fades: a quitter's 97 % falls below half within 16 blocks of leaving | `reputation_sim/reputation_sim.json` (series "Quitter") |
| An offline 18× whale is capped at 30 % and finality keeps going | `02_demo_output.txt` stage 7; `screenshots/04_whale_capped.png` |
| The cap never more than doubles anyone else's share | `reputation_sim/fig_cap.png`; `01_unit_tests.txt` (`test_cap_never_more_than_doubles_anyone_else`); `REVIEW.md` |
| All nodes hold the identical finalized history | `02_demo_output.txt` stage 8 |
| A Byzantine validator that signs two blocks for one slot is caught and slashed | `03_attack_demo_output.txt`; `ATTACK` line in `logs/attack/node4.log`; `CAUGHT` and `SLASHED` lines in `logs/attack/node1-3.log`; `screenshots/05_byzantine_slashed.png` |
| No conflicting block is ever finalized during the attack | `03_attack_demo_output.txt` stage 4 |
| 446–509 transfers per second; 500-transaction blocks | `04_benchmark_output.txt`; `benchmark/fig_throughput.png` |
| Median finality latency 1.0 s | `04_benchmark_output.txt`; `benchmark/fig_finality.png` |
| Restarted validator in sync 0.71 s after restart | `04_benchmark_output.txt`; `benchmark/fig_recovery.png` |
| Independent review findings and fixes | `REVIEW.md` |

To regenerate everything, run these from inside `NyxChain/`:

- `python -m pytest -v`
- `python scripts/demo.py`
- `python scripts/attack_demo.py`
- `python scripts/reputation_sim.py --out sim_out`
- `python scripts/benchmark.py --out benchmark_out`

To have the demos save dashboard screenshots, set the environment variable `NYX_SHOTS` to a folder. This needs Google Chrome.
