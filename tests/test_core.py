import pytest

from nyx.block import InvalidBlock, merkle_root
from nyx.chain import Chain
from nyx.crypto import KeyPair, is_address
from nyx.mempool import Mempool
from nyx.params import BLOCK_REWARD, REP_GENESIS, REP_MAX, REP_MIN, REP_NEW, UNBOND_BLOCKS, VOTE_DEADLINE
from nyx.state import State, cap_weights, rep_sample
from nyx.tx import InvalidTx, Transaction
from nyx.vote import InvalidVote, Vote, check_evidence

GENESIS_TIME = 1_000_000


class Clock:
    def __init__(self):
        self.ms = GENESIS_TIME * 1000

    def __call__(self):
        return self.ms

    def to_slot(self, slot, slot_ms=2000):
        self.ms = GENESIS_TIME * 1000 + slot * slot_ms + 100


@pytest.fixture
def net():
    vals = [KeyPair.generate() for _ in range(3)]
    alice, bob = KeyPair.generate(), KeyPair.generate()
    genesis = {
        "chain_id": "test",
        "genesis_time": GENESIS_TIME,
        "slot_seconds": 2,
        "balances": {alice.address: 1000, **{v.address: 100 for v in vals}},
        "stakes": {v.address: 500 for v in vals},
    }
    clock = Clock()
    chain = Chain(genesis, clock=clock)
    by_addr = {v.address: v for v in vals}
    return chain, clock, vals, by_addr, alice, bob


def produce(chain, clock, by_addr, slot, txs=(), votes=(), evidence=()):
    clock.to_slot(slot)
    kp = by_addr[chain.proposer_for(slot)]
    return chain.build_block(kp, slot, list(txs), list(votes), list(evidence))


def advance(chain, clock, by_addr, n, start_slot=None, votes_for=None):
    """Produce n blocks on the tip. votes_for(block) -> list of vote dicts to include in the next block."""
    slot = (start_slot or chain.tip_block.slot + 1)
    pending = []
    for _ in range(n):
        b = produce(chain, clock, by_addr, slot, votes=pending)
        assert chain.add_block(b) == "extended"
        pending = votes_for(b) if votes_for else []
        slot += 1
    return pending


def vote(kp, block):
    return Vote(block.height, block.hash, kp.public_hex).sign(kp).to_dict()


def transfer(kp, to, amount, nonce, fee=1):
    return Transaction("transfer", kp.public_hex, to.address, amount, fee, nonce).sign(kp)


# --- crypto / tx -------------------------------------------------------------
def test_signatures_and_addresses():
    kp = KeyPair.generate()
    assert is_address(kp.address)
    t = transfer(kp, KeyPair.generate(), 5, 0)
    assert t.signature_valid()
    t.amount = 6  # tamper
    assert not t.signature_valid()


def test_merkle_root_changes_with_order():
    a, b = "aa" * 32, "bb" * 32
    assert merkle_root([a, b]) != merkle_root([b, a])


# --- state ---------------------------------------------------------------------
def test_transfer_and_nonce_replay(net):
    chain, _, _, _, alice, bob = net
    st = chain.state.copy()
    t = transfer(alice, bob, 100, 0)
    assert st.apply_tx(t) == 1
    assert st.balance(alice.address) == 899 and st.balance(bob.address) == 100
    with pytest.raises(InvalidTx, match="nonce"):
        st.apply_tx(t)


def test_insufficient_balance(net):
    chain, _, _, _, alice, bob = net
    with pytest.raises(InvalidTx, match="insufficient"):
        chain.state.copy().apply_tx(transfer(alice, bob, 5000, 0))


def test_new_staker_starts_with_low_reputation(net):
    chain, _, vals, _, alice, _ = net
    st = chain.state.copy()
    st.apply_tx(Transaction("stake", alice.public_hex, "", 500, 1, 0).sign(alice))
    assert st.rep(alice.address) == REP_NEW
    assert st.rep(vals[0].address) == REP_GENESIS
    # Same stake, less earned trust -> less voting power.
    assert st.weight(alice.address) < st.weight(vals[0].address)


def test_unstake_goes_through_unbonding(net):
    chain, clock, vals, by_addr, _, _ = net
    v = vals[0]
    tx = Transaction("unstake", v.public_hex, "", 500, 1, 0).sign(v)
    b = produce(chain, clock, by_addr, 1, [tx])
    chain.add_block(b)
    st = chain.state
    assert st.stake(v.address) == 0
    assert st.unbonding_total(v.address) == 500
    assert v.address not in dict(st.validators())
    before = st.balance(v.address)
    # Coins come back only once the unbonding period has passed.
    alive = {a: k for a, k in by_addr.items() if a != v.address}
    slot = 2
    while chain.height < 1 + UNBOND_BLOCKS:
        if chain.proposer_for(slot) in alive:
            chain.add_block(produce(chain, clock, alive, slot))
        slot += 1
    assert chain.state.unbonding_total(v.address) == 0
    assert chain.state.balance(v.address) >= before + 500


def test_supply_is_conserved(net):
    chain, clock, _, by_addr, alice, bob = net
    supply0 = chain.state.total_supply()
    chain.add_block(produce(chain, clock, by_addr, 1, [transfer(alice, bob, 50, 0)]))
    chain.add_block(produce(chain, clock, by_addr, 2))
    # Only block rewards create coins; fees move from sender to proposer.
    assert chain.state.total_supply() == supply0 + 2 * BLOCK_REWARD


# --- blocks ----------------------------------------------------------------------
def test_valid_block_is_accepted_and_rewarded(net):
    chain, clock, _, by_addr, alice, bob = net
    b = produce(chain, clock, by_addr, 1, [transfer(alice, bob, 50, 0)])
    assert chain.add_block(b) == "extended"
    assert chain.height == 1
    assert chain.state.balance(bob.address) == 50
    assert chain.state.balance(b.proposer_address) == 100 + BLOCK_REWARD + 1
    assert chain.state.rep(b.proposer_address) == rep_sample(REP_GENESIS, True)   # one successful duty


def test_wrong_proposer_rejected(net):
    chain, clock, vals, _, _, _ = net
    clock.to_slot(1)
    right = chain.proposer_for(1)
    wrong = next(v for v in vals if v.address != right)
    with pytest.raises(InvalidBlock, match="proposer"):
        chain.add_block(chain.build_block(wrong, 1, []))


def test_tampered_block_rejected(net):
    chain, clock, _, by_addr, alice, bob = net
    b = produce(chain, clock, by_addr, 1, [transfer(alice, bob, 50, 0)])
    b.txs[0].amount = 500
    with pytest.raises(InvalidBlock):
        chain.add_block(b)


def test_future_block_rejected(net):
    chain, clock, _, by_addr, _, _ = net
    b = produce(chain, clock, by_addr, 10)
    clock.to_slot(3)
    with pytest.raises(InvalidBlock, match="future"):
        chain.add_block(b)


def test_missed_slot_penalises_absent_proposer(net):
    chain, clock, _, by_addr, _, _ = net
    absent = chain.proposer_for(1)
    s = 2
    while chain.proposer_for(s) == absent:
        s += 1
    rep_before = chain.state.rep(absent)
    assert chain.add_block(produce(chain, clock, by_addr, s)) == "extended"
    assert chain.state.rep(absent) < rep_before
    assert any(a == absent for _, a in chain.events[chain.tip]["missed"])


def run_blocks(chain, clock, keys, n, voters, start_slot=None):
    """Produce n slots on the tip. A slot is filled only if its proposer is in `keys`.
    Every key in `voters` votes for each new block; the votes go into the next block."""
    slot = start_slot or chain.tip_block.slot + 1
    pending = []
    for _ in range(n):
        p = chain.proposer_for(slot)
        if p in keys:
            clock.to_slot(slot)
            b = chain.build_block(keys[p], slot, [], pending)
            assert chain.add_block(b) == "extended"
            pending = [vote(k, b) for k in voters]
        slot += 1
    return slot


def test_dead_validator_loses_influence(net):
    """Kill one validator: its reliability collapses while the chain keeps going."""
    chain, clock, vals, by_addr, _, _ = net
    dead = vals[0].address
    alive = {a: k for a, k in by_addr.items() if a != dead}
    w0 = chain.state.weight(dead)
    run_blocks(chain, clock, alive, 150, list(alive.values()), start_slot=1)
    assert chain.height > 80
    missed = sum(1 for b in chain.canonical() for _, a in chain.events[b.hash]["missed"] if a == dead)
    assert missed >= 2
    assert chain.state.weight(dead) < w0 / 3
    assert all(chain.state.rep(a) > 8_500 for a in alive)


# --- reputation = recent reliability -------------------------------------------
def test_reputation_is_a_moving_average():
    r = REP_MAX
    for _ in range(50):
        r = rep_sample(r, False)
    assert 4_800 <= r <= 5_200            # half-life: 50 failed duties halve it
    r = REP_GENESIS
    for _ in range(1000):
        r = rep_sample(r, True)
    assert r == REP_MAX                   # steady good work reaches exactly 100%
    r = 600
    for _ in range(100):
        r = rep_sample(r, False)
    assert r == REP_MIN                   # never below the floor, so a node can come back


def test_equal_work_gives_equal_reputation(net):
    """Teammate's rule: a long or early history gives no lasting edge. Same recent work -> same reputation."""
    chain, clock, vals, by_addr, alice, _ = net
    stake_tx = Transaction("stake", alice.public_hex, "", 500, 1, 0).sign(alice)
    clock.to_slot(1)
    chain.add_block(chain.build_block(by_addr[chain.proposer_for(1)], 1, [stake_tx]))
    assert chain.state.rep(alice.address) == REP_NEW          # starts at 10%, genesis at ~50%
    keys = dict(by_addr, **{alice.address: alice})
    run_blocks(chain, clock, keys, 300, list(keys.values()))
    reps = [chain.state.rep(a) for a in keys]
    assert min(reps) > 9_000
    assert max(reps) - min(reps) < 300                         # within 3 points of each other


def test_old_work_fades(net):
    chain, clock, vals, by_addr, _, _ = net
    slot = run_blocks(chain, clock, by_addr, 120, list(by_addr.values()), start_slot=1)
    quitter = vals[0].address
    before = chain.state.rep(quitter)
    alive = {a: k for a, k in by_addr.items() if a != quitter}
    run_blocks(chain, clock, alive, 60, list(alive.values()), start_slot=slot)
    after = chain.state.rep(quitter)
    assert before > 8_500 and after < before / 2               # good history does not protect a quitter


def test_vote_deadline(net):
    chain, clock, vals, by_addr, _, _ = net
    b1 = produce(chain, clock, by_addr, 1)
    chain.add_block(b1)
    late = vals[0]
    on_time = [vote(v, b1) for v in vals[1:]]
    # Votes for b1 from vals[1:] arrive in block 2; vals[0]'s vote arrives two blocks later (still in time).
    chain.add_block(produce(chain, clock, by_addr, 2, votes=on_time))
    chain.add_block(produce(chain, clock, by_addr, 3))
    chain.add_block(produce(chain, clock, by_addr, 4, votes=[vote(late, b1)]))
    assert chain.height == 1 + VOTE_DEADLINE
    assert chain.events[chain.tip]["late"] == []                # everyone's vote for #1 landed by #4
    # Nobody voted for block 2 at all: at block 5 every validator is debited.
    chain.add_block(produce(chain, clock, by_addr, 5))
    assert sorted(chain.events[chain.tip]["late"]) == sorted(v.address for v in vals)


# --- voting-weight cap ------------------------------------------------------------
def shares(weights):
    total = sum(w for _, w in weights)
    return [w / total for _, w in weights]


def test_cap_limits_a_whale_to_30_percent():
    raw = [("whale", 2000), ("a", 500), ("b", 500), ("c", 500), ("d", 500)]   # whale has 50% of raw weight
    capped = cap_weights(raw)
    assert max(shares(capped)) <= 0.30 + 1e-9
    assert dict(capped)["a"] == 500                            # small validators untouched
    assert shares(capped)[0] > 0.29                            # the whale still gets its full 30%


def test_cap_handles_several_whales_and_small_sets():
    capped = cap_weights([("w1", 1500), ("w2", 1400), ("a", 400), ("b", 400), ("c", 400), ("d", 400)])
    assert max(shares(capped)) <= 0.30 + 1e-9
    assert dict(capped)["w1"] == dict(capped)["w2"]
    fair = [("a", 300), ("b", 290), ("c", 280), ("d", 270), ("e", 260)]
    assert cap_weights(fair) == fair                           # nothing to cap
    three = [("a", 900), ("b", 100), ("c", 100)]
    assert cap_weights(three) == three                         # a 30% cap is impossible with 3 validators
    four = cap_weights([("a", 400), ("b", 100), ("c", 120), ("d", 80)])
    assert max(shares(four)) <= 0.30 + 1e-9 and len({w for _, w in four}) == 4   # capped, not flattened


def test_cap_never_more_than_doubles_anyone_else():
    """Regression for the review: plain water-filling gave 2 tiny sybils 40% of the vote."""
    sybil = cap_weights([("h1", 99_000), ("h2", 99_000), ("s1", 10), ("s2", 10)])
    assert sum(shares(sybil)[2:]) < 0.001                      # attacker with 0.01% of weight stays at ~0%
    dead = cap_weights([("A", 9_900), ("B", 9_900), ("C", 500), ("D", 99)])     # C crashed to 5% reputation
    assert shares(dead)[2] < 0.05                              # a dead validator is not lifted back up
    newcomer = cap_weights([("a", 496), ("b", 496), ("c", 496), ("n", 10)])
    assert shares(newcomer)[3] <= 2 * 10 / (3 * 496 + 10) + 1e-9   # a newcomer's share at most doubles


def run_whale_network(clock, normal, whale_kp, whale_stake):
    keys = normal + [whale_kp]
    genesis = {"chain_id": "cap", "genesis_time": GENESIS_TIME, "slot_seconds": 2,
               "balances": {}, "stakes": {k.address: 500 for k in normal}}
    genesis["stakes"][whale_kp.address] = whale_stake
    return Chain(genesis, clock=clock)


def test_cap_applies_to_the_lottery_and_finality():
    clock = Clock()
    normal = [KeyPair.generate() for _ in range(4)]
    whale = KeyPair.generate()
    chain = run_whale_network(clock, normal, whale, 1500)      # 43% of raw weight
    st = chain.state
    assert st.raw_weight(whale.address) * 100 // sum(st.raw_weight(k.address) for k in normal + [whale]) >= 42
    assert st.weight(whale.address) * 10 <= st.total_weight() * 3
    # The lottery follows the capped weight: over many slots the whale wins about 30%, not 43%.
    wins = sum(chain.proposer_for(s) == whale.address for s in range(1, 4001))
    assert 0.26 < wins / 4000 < 0.34
    # The whale never shows up; the others alone keep finalizing.
    alive = {k.address: k for k in normal}
    run_blocks(chain, clock, alive, 40, normal, start_slot=1)
    assert chain.finalized_height >= chain.height - 3


def test_newcomer_not_charged_before_joining(net):
    chain, clock, vals, by_addr, alice, _ = net
    keys = dict(by_addr)
    slot = run_blocks(chain, clock, keys, 5, list(keys.values()), start_slot=1)
    clock.to_slot(slot)
    tx = Transaction("stake", alice.public_hex, "", 500, 1, 0).sign(alice)
    chain.add_block(chain.build_block(by_addr[chain.proposer_for(slot)], slot, [tx]))
    joined = chain.height
    keys[alice.address] = alice
    run_blocks(chain, clock, keys, 3, list(by_addr.values()), start_slot=slot + 1)   # alice does not vote yet
    late_heights = [chain.events[b.hash]["late"] for b in chain.canonical() if b.height <= joined + 3]
    assert all(alice.address not in late for late in late_heights)   # blocks before she joined are not her duty


# --- finality --------------------------------------------------------------------
def test_two_thirds_of_votes_finalize_a_block(net):
    chain, clock, vals, by_addr, _, _ = net
    b1 = produce(chain, clock, by_addr, 1)
    chain.add_block(b1)
    st = chain.state
    order = sorted(vals, key=lambda v: st.weight(v.address))
    # One vote out of three (the lightest validator) is not enough.
    chain.add_block(produce(chain, clock, by_addr, 2, votes=[vote(order[0], b1)]))
    assert chain.finalized_height == 0
    # The other two votes cross 2/3 of total weight.
    chain.add_block(produce(chain, clock, by_addr, 3, votes=[vote(order[1], b1), vote(order[2], b1)]))
    assert chain.finalized_height == 1 and chain.finalized_hash == b1.hash
    assert chain.events[chain.tip]["finalized"] == (1, b1.hash)


def test_votes_are_checked(net):
    chain, clock, vals, by_addr, alice, _ = net
    b1 = produce(chain, clock, by_addr, 1)
    chain.add_block(b1)
    bad = vote(vals[0], b1)
    bad["block"] = "ab" * 32           # signed for a different block
    with pytest.raises(InvalidBlock, match="vote"):
        chain.add_block(produce(chain, clock, by_addr, 2, votes=[bad]))
    outsider = vote(alice, b1)          # not a validator
    with pytest.raises(InvalidBlock, match="vote"):
        chain.add_block(produce(chain, clock, by_addr, 2, votes=[outsider]))
    dup = vote(vals[0], b1)             # counted twice in one block
    with pytest.raises(InvalidBlock, match="vote"):
        chain.add_block(produce(chain, clock, by_addr, 2, votes=[dup, dup]))


def test_finalized_block_cannot_be_reverted(net):
    chain, clock, vals, by_addr, _, _ = net
    every = lambda b: [vote(v, b) for v in vals]  # noqa: E731
    advance(chain, clock, by_addr, 4, start_slot=1, votes_for=every)
    assert chain.finalized_height >= 2
    # A competing branch that forks from genesis must be refused, however long it gets.
    other = Chain(chain.genesis, clock=clock)
    rival = produce(other, clock, by_addr, 20)
    other.add_block(rival)
    with pytest.raises(InvalidBlock, match="finalized"):
        chain.add_block(rival)


def test_fork_choice_prefers_more_finality(net):
    chain, clock, vals, by_addr, _, _ = net
    b1 = produce(chain, clock, by_addr, 1)
    chain.add_block(b1)
    # Branch X: longer, but no votes.  Branch Y: shorter, but finalizes b1's child.
    x = Chain(chain.genesis, clock=clock)
    x.add_block(b1)
    for s in (2, 3, 4):
        x.add_block(produce(x, clock, by_addr, s))
    y = Chain(chain.genesis, clock=clock)
    y.add_block(b1)
    y.add_block(produce(y, clock, by_addr, 5, votes=[vote(v, b1) for v in vals]))
    for b in x.canonical()[2:]:
        chain.add_block(b)
    assert chain.height == 4
    assert chain.add_block(y.tip_block) == "reorg"
    assert chain.finalized_height == 1 and chain.tip == y.tip


# --- slashing --------------------------------------------------------------------
def test_double_vote_is_slashed(net):
    chain, clock, vals, by_addr, _, _ = net
    b1 = produce(chain, clock, by_addr, 1)
    other = Chain(chain.genesis, clock=clock)
    b1x = produce(other, clock, by_addr, 2)
    chain.add_block(b1)
    cheat = vals[0]
    ev = {"kind": "double_vote", "a": vote(cheat, b1), "b": vote(cheat, b1x)}
    assert check_evidence(ev)[0] == cheat.address
    stake0 = chain.state.stake(cheat.address)
    b2 = produce(chain, clock, by_addr, 3, evidence=[ev])
    chain.add_block(b2)
    st = chain.state
    assert st.stake(cheat.address) == stake0 // 2
    assert cheat.address in st.slashed and st.weight(cheat.address) == 0
    assert cheat.address not in dict(st.validators())
    # The same offender cannot be slashed twice.
    with pytest.raises(InvalidBlock, match="already slashed"):
        chain.add_block(produce(chain, clock, by_addr, 4, evidence=[ev]))


def test_double_block_is_slashed(net):
    chain, clock, vals, by_addr, _, _ = net
    clock.to_slot(1)
    p = by_addr[chain.proposer_for(1)]
    a = chain.build_block(p, 1, [])
    b = chain.build_block(p, 1, [], timestamp_ms=a.timestamp_ms + 1)
    chain.add_block(a)
    ev = {"kind": "double_block", "a": a.signed_header(), "b": b.signed_header()}
    assert check_evidence(ev)[0] == p.address
    honest = {k: v for k, v in by_addr.items() if k != p.address}
    s = 2
    while chain.proposer_for(s) not in honest:
        s += 1
    chain.add_block(produce(chain, clock, by_addr, s, evidence=[ev]))
    assert p.address in chain.state.slashed


def test_fake_evidence_rejected(net):
    chain, clock, vals, by_addr, _, _ = net
    b1 = produce(chain, clock, by_addr, 1)
    chain.add_block(b1)
    same = vote(vals[0], b1)
    with pytest.raises(InvalidVote):
        check_evidence({"kind": "double_vote", "a": same, "b": same})
    forged = dict(same, block="cd" * 32)  # signature does not match
    with pytest.raises(InvalidBlock, match="evidence"):
        chain.add_block(produce(chain, clock, by_addr, 2, evidence=[{"kind": "double_vote", "a": same, "b": forged}]))


# --- mempool -------------------------------------------------------------------
def test_mempool_orders_by_nonce_and_skips_bad(net):
    chain, _, _, _, alice, bob = net
    mp = Mempool()
    for n in (2, 0, 1):
        mp.add(transfer(alice, bob, 10, n), chain.state)
    assert mp.next_nonce(alice.address, chain.state) == 3
    picked = mp.select(chain.state, 10)
    assert [t.nonce for t in picked] == [0, 1, 2]
    with pytest.raises(InvalidTx):
        mp.add(transfer(alice, bob, 10, 0, fee=0), chain.state)
