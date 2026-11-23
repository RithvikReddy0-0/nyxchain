# NyxChain

A Layer-1 blockchain built from scratch in Python: its own block format, peer-to-peer network, transaction pool, wallet, live dashboard, and a consensus protocol where voting power is **stake × reputation**, reputation means **recent reliability** (old work fades), no validator may hold more than **30 %** of the vote, blocks become final after **⅔ of the weight** votes for them, and cheaters are **slashed**.

Only runtime dependency: `cryptography` (Ed25519 signatures). Python 3.9 or newer, Windows, macOS or Linux.

**Paper:** [`docs/NyxChain_IEEE_Paper.pdf`](docs/NyxChain_IEEE_Paper.pdf) (IEEE format, LaTeX source in `docs/paper_source/`) · **Literature survey:** [`docs/NyxChain_Literature_Survey.pdf`](docs/NyxChain_Literature_Survey.pdf) · **Evidence:** [`proof/PROOF_INDEX.md`](proof/PROOF_INDEX.md)

**Team:** Aman Aggarwal (BL.EN.U4CSE23206), Harsh Vardan Singh (BL.EN.U4CSE23222), Mukkara Rithvik Reddy (BL.EN.U4CSE23264), Dept. of CSE, Amrita School of Computing, Bengaluru.

## Quick start

```bash
pip install -r requirements.txt

python scripts/demo.py           # 4 validators: payments, staking, crash + recovery, reputation, whale cap (~2 min)
python scripts/attack_demo.py    # a Byzantine validator signs two blocks for one slot and is slashed (~40 s)
python scripts/benchmark.py      # throughput, finality latency, crash-recovery time + charts (~2 min)
python scripts/reputation_sim.py # 300-block simulation of the reputation rule and the cap + charts (~5 s)
python -m pytest -q              # 30 unit tests
```

On Windows you can also double-click `run_demo.bat`.

### Run a network by hand and open the dashboard

```bash
python -m nyx testnet --validators 4 --out devnet       # keys, genesis, node configs
python -m nyx node --config devnet/node1.json           # one terminal per node
python -m nyx node --config devnet/node2.json
python -m nyx node --config devnet/node3.json
python -m nyx node --config devnet/node4.json
```

Open **http://127.0.0.1:8001/** for the live explorer (network view, validators, blocks, finality, consensus events). Then use the wallet:

```bash
python -m nyx status
python -m nyx send --key devnet/keys/alice.json --to <bob-address> --amount 50
python -m nyx balance <address>
python -m nyx stake   --key devnet/keys/bob.json --amount 200
python -m nyx unstake --key devnet/keys/bob.json --amount 100
python -m nyx --node http://127.0.0.1:8002 blocks       # ask a different node
```

Kill any node window (Ctrl+C) and watch the dashboard: the node goes offline, the chain keeps finalizing, and that validator's reputation falls. Start it again and it catches up in under a second.

## How it works

| Layer | File | What it does |
|---|---|---|
| Crypto | `nyx/crypto.py` | Ed25519 keys, `nyx…` addresses (SHA-256 of the public key), canonical JSON, signature cache |
| Transactions | `nyx/tx.py` | `transfer`, `stake`, `unstake`; signed, with per-account nonces against replay |
| Votes & evidence | `nyx/vote.py` | Signed finality votes; double-vote and double-block proofs |
| Blocks | `nyx/block.py` | Header (height, parent, slot, timestamp, proposer, Merkle root over txs + votes + evidence), signed by the proposer |
| State | `nyx/state.py` | Balances, nonces, stakes, unbonding, reputation, votes, finality, slashing |
| Consensus | `nyx/consensus.py` | Slot clock and the weighted proposer lottery |
| Chain | `nyx/chain.py` | Full validation, rewards and penalties, finality, fork choice, reorgs |
| Mempool | `nyx/mempool.py` | Pending txs, votes and evidence; block packing |
| P2P | `nyx/p2p.py` | TCP + JSON-lines gossip, peer exchange from one seed, chain sync, automatic redial |
| Node | `nyx/node.py` | Block production, voting, equivocation detection, persistence, HTTP API, dashboard |
| Dashboard | `nyx/static/dashboard.html` | Live block explorer served by every node |
| CLI / wallet | `nyx/cli.py` | Node runner, testnet generator, wallet commands |

### Consensus

1. **Slots.** Time is split into fixed slots (2 s by default, 1 s in the demos). For each slot exactly one validator may propose, picked by a deterministic lottery weighted by `stake × reputation` and seeded by (parent hash, slot). Every node computes the same answer.
2. **Reputation = recent reliability (0–100 %).** Every duty is one sample in a moving average: getting your vote for block *h* on chain by block *h*+3, and proposing when chosen. A missed slot counts as 5 failed duties because it costs the whole network a block. Each sample fades by half after 50 newer ones, so a long or early history gives no lasting edge: validators that do the same work end up with the same score, and anyone who stops working loses trust within a few dozen blocks. Genesis validators start at 50 %; anyone who stakes later starts at **10 %**, so influence cannot be bought instantly. Floor 5 %, so a recovered node can always earn its way back.
3. **30 % cap.** The largest validators are pulled down so none holds more than 30 % of the voting weight (water-filling; 4+ validators). Finality stalls only if more than ⅓ of weight is offline, so a validator below that cannot stall finality alone. Capping raises everyone else's share, so the cap may **never more than double anyone else's share**: tiny or crashed validators are never handed real power. A single validator is held to 30 % while it owns under about 65 % of all raw weight; beyond that the cap loosens.
4. **Finality.** After accepting a new block, each validator signs a vote for it (at most one per height). Votes are gossiped and packed into later blocks. When votes from **≥ ⅔ of total weight** are on chain for a block, it is **final**: no node will ever accept a branch that does not contain it.
5. **Fork choice.** Prefer the branch with the highest finalized block, then the longest, then the lowest hash.
6. **Slashing.** Voting for two different blocks at one height, or signing two blocks for one slot, is equivocation. Any node that sees both signed messages builds the proof; the next proposer puts it on chain. The offender loses **50 %** of its stake and unbonding coins (90 % burned, 10 % to the reporter), drops to 0 reputation and is removed from the validator set for good.
7. **Unbonding.** Unstaked coins stay locked, and slashable, for 10 blocks before they return to the balance.
8. **Self-healing.** A dead validator misses its votes and slots, so its reliability score falls block by block and its share shrinks. On restart it reloads its blocks from disk and syncs the rest from peers.

### HTTP API (per node)

`GET /` dashboard · `GET /status` · `GET /events` · `GET /account/<address>` · `GET /blocks?from=&limit=` · `GET /block/<height>` · `POST /tx` · `POST /txs` (batch)

## Results (measured on one machine, 4 validators, 1-second slots)

See `proof/` for full outputs, logs, screenshots and charts.

- 30/30 unit tests, 20/20 demo checks, 9/9 attack checks pass.
- Throughput: 446–509 transfers/s, limited by the 500-transaction block cap at one block per second.
- Finality: 1.0 s median, about one slot after a block appears.
- A crashed validator is back in sync 0.71 s after restart.
- Same work, same reputation: running validators end within 2 points of each other; a staker that never runs a node fades.
- An offline whale staking 18× a normal validator is capped at 30 % and finality keeps going.
- An independent code review found one design flaw in the first version of the cap; it is fixed and covered by regression tests (`proof/REVIEW.md`).

## Limitations

- Throughput is capped by design (500 txs per 1-second block); the network has only been run on one machine.
- Slot timing assumes roughly synchronised clocks (±1 s tolerance).
- The cap cannot stop a validator that owns most of all stake and is fully reliable; it loosens above about 65 % of raw weight rather than inflate everyone else.
- After a reorg an honest validator can lose one reputation sample; no state root in block headers yet.
- Finality is single-round (no separate justify/finalize steps as in Casper FFG), which assumes fewer than ⅓ of weight is Byzantine and that honest validators vote once per height.
- No transaction fees market, no smart contracts, no light clients.
