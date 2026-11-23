"""The block tree: validation, rewards and penalties, finality, and fork choice."""
import time

from .block import Block, InvalidBlock, body_root, make_genesis_block
from .consensus import current_slot, missed_proposers, select_proposer, slot_start_ms
from .params import (
    BLOCK_REWARD,
    MAX_EVIDENCE_PER_BLOCK,
    MAX_TXS_PER_BLOCK,
    MAX_VOTES_PER_BLOCK,
    MISSED_SLOT_SAMPLES,
    STATE_HISTORY,
    VOTE_DEADLINE,
    VOTE_WINDOW,
    WHISTLEBLOWER_DEN,
    WHISTLEBLOWER_NUM,
)
from .state import State
from .tx import InvalidTx
from .vote import InvalidVote, Vote, check_evidence

CLOCK_TOLERANCE_MS = 1_000


class Chain:
    def __init__(self, genesis: dict, clock=None):
        self.genesis = genesis
        self.slot_ms = int(genesis.get("slot_seconds", 2) * 1000)
        self.genesis_ms = int(genesis["genesis_time"] * 1000)
        self.clock = clock or (lambda: int(time.time() * 1000))

        g = make_genesis_block(genesis)
        self.genesis_hash = g.hash
        self.blocks = {g.hash: g}
        self.states = {g.hash: State.from_genesis(genesis, g.hash)}
        self.events = {g.hash: {"missed": [], "slashed": [], "finalized": None, "votes": 0, "late": []}}
        self.tip = g.hash
        self._pending_events = None

    # --- views -------------------------------------------------------------
    @property
    def tip_block(self) -> Block:
        return self.blocks[self.tip]

    @property
    def height(self) -> int:
        return self.tip_block.height

    @property
    def state(self) -> State:
        return self.states[self.tip]

    @property
    def finalized_height(self) -> int:
        return self.state.finalized_height

    @property
    def finalized_hash(self) -> str:
        return self.state.finalized_hash

    def current_slot(self) -> int:
        return current_slot(self.clock(), self.genesis_ms, self.slot_ms)

    def slot_start(self, slot: int) -> int:
        return slot_start_ms(slot, self.genesis_ms, self.slot_ms)

    def proposer_for(self, slot: int, parent_hash: str = None):
        parent_hash = parent_hash or self.tip
        return select_proposer(self.states[parent_hash], parent_hash, slot)

    def canonical(self) -> list:
        out, h = [], self.tip
        while True:
            b = self.blocks[h]
            out.append(b)
            if b.height == 0:
                break
            h = b.prev_hash
        out.reverse()
        return out

    def canonical_range(self, start: int, limit: int) -> list:
        return self.canonical()[max(0, start): max(0, start) + limit]

    def ancestors(self, block_hash: str, depth: int) -> dict:
        """hash -> height for the block itself and up to `depth` ancestors."""
        out, h = {}, block_hash
        for _ in range(depth + 1):
            b = self.blocks.get(h)
            if b is None:
                break
            out[h] = b.height
            if b.height == 0:
                break
            h = b.prev_hash
        return out

    def descends_from(self, block: Block, anc_hash: str) -> bool:
        anc = self.blocks[anc_hash]
        if block.hash == anc_hash:
            return True
        h = block.prev_hash
        while True:
            b = self.blocks.get(h)
            if b is None or b.height < anc.height:
                return False
            if b.hash == anc_hash:
                return True
            if b.height == anc.height:
                return False
            h = b.prev_hash

    # --- packing helpers (used by the proposer and by validation) ----------
    def vote_ok(self, st: State, anc: dict, v: Vote) -> bool:
        """Can this vote be counted in a block whose ancestors are `anc` and pre-state is `st`?"""
        if not v.valid():
            return False
        if anc.get(v.block_hash) != v.height:        # must be a recent ancestor on this branch
            return False
        if st.weight(v.address) == 0:
            return False
        # Votes for already-final blocks still count towards the voter's reputation, so a
        # validator whose vote arrives just after finality is not punished as late.
        return v.address not in st.vote_log.get(v.height, ())

    def pick_votes(self, vote_dicts: list, exclude=()) -> list:
        """Votes that a block built on the tip may count. `exclude`: validators slashed in that same block."""
        st, anc = self.state, self.ancestors(self.tip, VOTE_WINDOW)
        out, seen = [], set()
        for d in vote_dicts:
            try:
                v = Vote.from_dict(d)
            except InvalidVote:
                continue
            key = (v.address, v.block_hash)
            if key not in seen and v.address not in exclude and self.vote_ok(st, anc, v):
                seen.add(key)
                out.append(v.to_dict())
                if len(out) >= MAX_VOTES_PER_BLOCK:
                    break
        return out

    def pick_evidence(self, evidence: list) -> list:
        st, out, offenders = self.state, [], set()
        for ev in evidence:
            try:
                who, _ = check_evidence(ev)
            except InvalidVote:
                continue
            if who in st.slashed or who in offenders:
                continue
            offenders.add(who)
            out.append(ev)
            if len(out) >= MAX_EVIDENCE_PER_BLOCK:
                break
        return out

    # --- validation --------------------------------------------------------
    def execute(self, block: Block) -> State:
        """Check a block against its parent and return the post-state. Raises InvalidBlock."""
        parent = self.blocks.get(block.prev_hash)
        if parent is None:
            raise InvalidBlock("unknown parent")
        pstate = self.states.get(block.prev_hash)
        if pstate is None:
            raise InvalidBlock("parent too old (state pruned)")

        if block.height != parent.height + 1:
            raise InvalidBlock("wrong height")
        if block.slot <= parent.slot:
            raise InvalidBlock("slot does not advance")
        if block.slot > self.current_slot() + 1:
            raise InvalidBlock("block from the future")
        start = self.slot_start(block.slot)
        if not (start - CLOCK_TOLERANCE_MS <= block.timestamp_ms <= start + self.slot_ms + CLOCK_TOLERANCE_MS):
            raise InvalidBlock("timestamp outside its slot")
        if len(block.txs) > MAX_TXS_PER_BLOCK:
            raise InvalidBlock("too many transactions")
        if len(block.votes) > MAX_VOTES_PER_BLOCK:
            raise InvalidBlock("too many votes")
        if len(block.evidence) > MAX_EVIDENCE_PER_BLOCK:
            raise InvalidBlock("too much evidence")
        if block.tx_root != body_root(block.txs, block.votes, block.evidence):
            raise InvalidBlock("body root mismatch")
        if not block.signature_valid():
            raise InvalidBlock("bad proposer signature")
        expected = select_proposer(pstate, parent.hash, block.slot)
        if block.proposer_address != expected:
            raise InvalidBlock(f"not this slot's proposer (expected {expected})")

        st = pstate.copy()
        ev_log = {"missed": [], "slashed": [], "finalized": None, "votes": 0, "late": []}

        # 1. Coins whose unbonding period is over become spendable.
        st.release_unbonded(block.height)

        # 2. Penalise everyone who was chosen for a skipped slot and did not show up.
        # A missed block costs the network a whole slot, so it weighs several failed duties.
        for slot, addr in missed_proposers(pstate, parent.hash, parent.slot, block.slot):
            st.record_duty(addr, False, MISSED_SLOT_SAMPLES)
            ev_log["missed"].append((slot, addr))

        # 3. Slashing evidence.
        for ev in block.evidence:
            try:
                who, what = check_evidence(ev)
            except InvalidVote as e:
                raise InvalidBlock(f"bad evidence: {e}")
            if who in st.slashed:
                raise InvalidBlock("evidence against an already slashed validator")
            cut = st.slash(who, block.height)
            bonus = cut * WHISTLEBLOWER_NUM // WHISTLEBLOWER_DEN
            st.reward(block.proposer_address, bonus)
            st.burned += cut - bonus
            ev_log["slashed"].append((who, cut, what))

        # 4. Finality votes.
        anc = self.ancestors(parent.hash, VOTE_WINDOW)
        seen = set()
        for d in block.votes:
            try:
                v = Vote.from_dict(d)
            except InvalidVote as e:
                raise InvalidBlock(str(e))
            if (v.address, v.block_hash) in seen or not self.vote_ok(st, anc, v):
                raise InvalidBlock(f"invalid vote by {v.address} for height {v.height}")
            seen.add((v.address, v.block_hash))
            st.add_vote(v.block_hash, v.height, v.address)
        ev_log["votes"] = len(seen)
        if st.update_finality():
            ev_log["finalized"] = (st.finalized_height, st.finalized_hash)

        # 5. Vote duty: every validator had to get a vote for block (h - VOTE_DEADLINE) on chain by now.
        #    Each on-time vote is a success sample in its reputation average, each missing vote a failure.
        due = block.height - VOTE_DEADLINE
        if due > 0:
            duty = [a for a, _ in pstate.validators()
                    if a not in st.slashed and st.active_since.get(a, 0) < due]
            done = st.vote_log.get(due, frozenset())
            ev_log["late"] = [a for a in duty if a not in done]
            st.score_votes(due, duty)

        # 6. Transactions.
        fees = 0
        for tx in block.txs:
            try:
                fees += st.apply_tx(tx, block.height)
            except InvalidTx as e:
                raise InvalidBlock(f"tx {tx.hash[:10]}: {e}")

        # 7. Reward the proposer: coins, and one successful duty for proposing.
        st.reward(block.proposer_address, BLOCK_REWARD + fees)
        st.record_duty(block.proposer_address, True)
        self._pending_events = ev_log
        return st

    # --- mutation ----------------------------------------------------------
    def add_block(self, block: Block) -> str:
        """Returns 'known', 'orphan', 'extended', 'reorg' or 'side'. Raises InvalidBlock."""
        h = block.hash
        if h in self.blocks:
            return "known"
        if block.prev_hash not in self.blocks:
            return "orphan"
        fin = self.blocks[self.finalized_hash]
        if block.height <= fin.height or not self.descends_from(block, fin.hash):
            raise InvalidBlock(f"conflicts with finalized block #{fin.height}")
        st = self.execute(block)
        self.blocks[h] = block
        self.states[h] = st
        self.events[h] = self._pending_events

        old_tip = self.tip
        if self._better(h, old_tip):
            self.tip = h
            self._prune()
            return "extended" if block.prev_hash == old_tip else "reorg"
        return "side"

    def _better(self, a: str, b: str) -> bool:
        """Fork choice: most finalized first, then longest, then lowest hash so every node agrees."""
        ka = (self.states[a].finalized_height, self.blocks[a].height)
        kb = (self.states[b].finalized_height, self.blocks[b].height)
        return ka > kb or (ka == kb and a < b)

    def _prune(self) -> None:
        floor = min(self.height - STATE_HISTORY, self.finalized_height - 1)
        if floor <= 0:
            return
        for h in [h for h, b in self.blocks.items() if b.height < floor and h in self.states]:
            del self.states[h]

    def build_block(self, kp, slot: int, txs: list, votes: list = (), evidence: list = (), timestamp_ms=None) -> Block:
        tip = self.tip_block
        start = self.slot_start(slot)
        ts = timestamp_ms if timestamp_ms is not None else min(max(self.clock(), start), start + self.slot_ms - 1)
        votes, evidence = list(votes), list(evidence)
        b = Block(
            height=tip.height + 1,
            prev_hash=tip.hash,
            slot=slot,
            timestamp_ms=ts,
            proposer=kp.public_hex,
            tx_root=body_root(txs, votes, evidence),
            txs=list(txs),
            votes=votes,
            evidence=evidence,
        )
        return b.sign(kp)
