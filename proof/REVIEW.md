# Independent review of the reputation and cap changes

After reputation was redefined as recent reliability and the 30 % voting-weight cap was added, a separate reviewer that had not seen the work being written read the code, ran the tests, and wrote its own fuzzing scripts (100,000+ random cases). It was told to be skeptical and to report anything that could fail in a viva.

## Findings and resolutions

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | **Bug** | Plain water-filling hands most of the vote to tiny or crashed validators whenever only one or two validators are large. Reproduced: two 100-NYX validators beside two 100,000-NYX validators got **40 %** of the vote; one honest whale plus three 100-NYX validators gave the small ones **70 %**; a crashed validator at the 5 % reputation floor was lifted back to the same weight as healthy ones. | **Fixed.** Boost limit: the cap may never more than double anyone else's share (`MAX_BOOST = 2`, `nyx/state.py: cap_weights`). The same cases now give the small validators 0.03 % and 0.09 % combined, and the crashed validator 4.9 %. All three are regression tests (`test_cap_never_more_than_doubles_anyone_else`). Re-fuzzed on 100,000 random weight lists: boost never above 2×, no weight ever raised, order kept, and a single dominant validator under 64 % of raw weight is always held to 30 %. |
| 2 | Correctness | A validator that stakes in block *h* was charged for votes on blocks *h*−2 and *h*−1, before it could vote. | **Fixed.** Activation height stored per validator; vote duties start after it (`test_newcomer_not_charged_before_joining`). |
| 3 | Correctness | After a reorg, votes already counted on the abandoned branch had been dropped from the mempool and could not be re-included. | **Fixed.** Votes stay in the mempool until they leave the 64-block window; duplicates are filtered when packing. |
| 3b | Limitation | After a reorg, an honest validator that already voted at that height cannot vote for the winning block without equivocating, so it loses one reputation sample. | Documented (report, Discussion). Rare: needs a fork at equal height. |
| 4 | Limitation | A proposer can build on its grandparent to orphan a rival's block when the lowest-hash tie-break favours it, costing the rival a missed slot. | Documented. Future work: proposer boost in fork choice. Not introduced by these changes. |
| 5 | Misleading claim | Integer rounding stopped reputation at 99.28 %, while comments said it approaches 100 %. | **Fixed.** The average is computed as "a success shrinks the gap to 100 % by d, a failure shrinks the gap to 0 % by d": identical maths, exact in integers. Steady work now reaches exactly 100 % (tested). |
| 6 | Misleading claim | The demo said the offline whale "would stall" finality without the cap, which depends on the numbers at that moment. | **Fixed.** The demo now computes the uncapped offline share and prints whether it is above one third (in the recorded run: 40 %, so yes). |
| 7 | Misleading claim | "Half-life about 50 blocks" holds only for validators with one duty per block. | **Fixed.** Comments and report say 50 duties, roughly 50 blocks for most validators. |
| 8 | Misleading claim | `consensus.py` still said the lottery is proportional to stake × reputation. | **Fixed.** It now says the capped voting weight. |
| 9 | Test gap | The cap test checked only `weight()`, not the lottery or finality. | **Fixed.** The test now runs the lottery over 4,000 slots (a 43 % whale wins about 30 %) and a 40-block chain where the whale never shows up and the others still finalize. |
| 10 | Nit | Block headers carry no state root, so a divergence between nodes would go undetected. | Documented as future work. The reviewer found no source of divergence. |

## Areas the reviewer checked and found sound

- **Determinism:** validators iterated in sorted order; all arithmetic in integers; every change to stakes, reputation or slashing resets the weight cache; block states share no mutable data.
- **Vote counting:** votes on side branches are rejected; no vote can count twice toward finality or reputation; votes for already-final blocks still earn their author reputation; vote-log pruning keeps every height a block can still reference.
- **Liveness:** with all honest validators online finality is always reachable; with 4+ validators, one dead validator cannot stall it alone.

## After the fixes

30/30 unit tests (stable over 20 runs), 20/20 demo checks, 9/9 attack checks, benchmark unchanged (446–509 tx/s, finality 1.0 s median, recovery 0.71 s). See the other files in this folder.
