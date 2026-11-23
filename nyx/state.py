"""Account state: balances, nonces, stakes, unbonding coins, reputation, votes and finality.

Every value is either immutable or copied in copy(), so a block's post-state
never shares mutable data with its parent's.
"""
from .params import (
    FINALITY_DEN,
    FINALITY_NUM,
    MAX_BOOST,
    MAX_SHARE_DEN,
    MAX_SHARE_NUM,
    MIN_STAKE,
    REP_DECAY_DEN,
    REP_DECAY_NUM,
    REP_GENESIS,
    REP_MAX,
    REP_MIN,
    REP_NEW,
    SLASH_DEN,
    SLASH_NUM,
    UNBOND_BLOCKS,
    VOTE_WINDOW,
)
from .tx import InvalidTx, Transaction


def rep_sample(rep: int, ok: bool) -> int:
    """One step of the reputation moving average, rep <- rep*d + 100%*(1-d)*outcome, floored at REP_MIN.

    Written as "a success closes the gap to 100% by a factor d, a failure closes the gap to 0%",
    which is the same average but stays exact in integers: steady good work reaches exactly 100%.
    """
    if ok:
        new = REP_MAX - (REP_MAX - rep) * REP_DECAY_NUM // REP_DECAY_DEN
    else:
        new = rep * REP_DECAY_NUM // REP_DECAY_DEN
    return max(REP_MIN, min(REP_MAX, new))


def _level_for(ws: list, target_total: int) -> int:
    """Smallest cap c with sum(min(w, c)) >= target_total, for weights sorted largest first."""
    n, rest = len(ws), sum(ws)
    for k in range(1, n + 1):                   # the k largest are at the cap
        rest -= ws[k - 1]
        c = -(-(target_total - rest) // k)      # ceil
        lower = ws[k] if k < n else 0
        if lower <= c <= ws[k - 1]:
            return c
    return ws[0]


def cap_weights(raw: list, num: int = MAX_SHARE_NUM, den: int = MAX_SHARE_DEN, boost: int = MAX_BOOST) -> list:
    """Pull the largest weights down to a common cap so none exceeds num/den of the capped total.

    raw: [(address, weight)] with weight > 0. Returns the same list with capped weights; order is
    kept, no weight is increased, and validators below the cap are untouched.

    1. Water-filling: with the k largest validators capped and the rest summing to R,
       c = (num/den) * (k*c + R), so c = R*num / (den - k*num).
    2. Capping shrinks the total, which raises everyone else's share by raw_total / capped_total.
       That factor is limited to `boost`, so the cap is raised if needed until the capped total is
       at least raw_total / boost. Without this, a network with one or two large validators would
       hand most of the weight to tiny or low-reputation ones.
    With n * num/den <= 1 (3 or fewer validators for 30%) the cap could only be met by making every
    weight equal, so weights are returned unchanged.
    """
    n = len(raw)
    if n * num <= den:
        return list(raw)
    total = sum(w for _, w in raw)
    if max(w for _, w in raw) * den <= total * num:
        return list(raw)
    ws = sorted((w for _, w in raw), reverse=True)
    rest = total
    cap = None
    for k in range(1, n):
        if den - k * num <= 0:
            break
        rest -= ws[k - 1]
        c = rest * num // (den - k * num)
        if ws[k] <= c <= ws[k - 1]:
            cap = c
            break
    if cap is None:
        return list(raw)
    cap = max(cap, _level_for(ws, -(-total // boost)))
    return [(a, min(w, cap)) for a, w in raw]


class State:
    def __init__(self):
        self.balances = {}
        self.nonces = {}
        self.stakes = {}
        self.reputation = {}
        self.unbonding = {}        # address -> tuple of (release_height, amount)
        self.slashed = {}          # address -> height at which it was slashed
        self.votes = {}            # target block hash -> (height, frozenset of voter addresses)
        self.vote_log = {}         # height -> frozenset of validators whose vote for that height was counted
        self.active_since = {}     # address -> height at which it became a validator (genesis: 0)
        self.finalized_height = 0
        self.finalized_hash = ""
        self.burned = 0
        self._vcache = None

    @classmethod
    def from_genesis(cls, genesis: dict, genesis_hash: str) -> "State":
        s = cls()
        s.balances = {a: int(v) for a, v in genesis.get("balances", {}).items()}
        s.stakes = {a: int(v) for a, v in genesis.get("stakes", {}).items()}
        s.reputation = {a: REP_GENESIS for a in s.stakes}
        s.active_since = {a: 0 for a in s.stakes}
        s.finalized_hash = genesis_hash
        return s

    def copy(self) -> "State":
        s = State()
        s.balances = dict(self.balances)
        s.nonces = dict(self.nonces)
        s.stakes = dict(self.stakes)
        s.reputation = dict(self.reputation)
        s.unbonding = dict(self.unbonding)
        s.slashed = dict(self.slashed)
        s.votes = dict(self.votes)
        s.vote_log = dict(self.vote_log)
        s.active_since = dict(self.active_since)
        s.finalized_height = self.finalized_height
        s.finalized_hash = self.finalized_hash
        s.burned = self.burned
        s._vcache = self._vcache       # immutable list; any change below resets it
        return s

    # --- reads -------------------------------------------------------------
    def balance(self, a: str) -> int:
        return self.balances.get(a, 0)

    def nonce(self, a: str) -> int:
        return self.nonces.get(a, 0)

    def stake(self, a: str) -> int:
        return self.stakes.get(a, 0)

    def rep(self, a: str) -> int:
        return self.reputation.get(a, REP_NEW)

    def unbonding_total(self, a: str) -> int:
        return sum(amt for _, amt in self.unbonding.get(a, ()))

    def raw_weight(self, a: str) -> int:
        """Stake scaled by reputation, before the per-validator cap."""
        if a in self.slashed or self.stake(a) < MIN_STAKE:
            return 0
        return self.stake(a) * self.rep(a) // REP_MAX

    def validators(self) -> list:
        """(address, voting weight) for every active validator, in a fixed order, after the cap."""
        if self._vcache is None:
            raw = [(a, self.raw_weight(a)) for a in sorted(self.stakes)]
            self._vcache = tuple(cap_weights([(a, w) for a, w in raw if w > 0]))
        return list(self._vcache)

    def weight(self, a: str) -> int:
        """Voting power: stake x reputation, capped so no one validator dominates."""
        for v, w in self.validators():
            if v == a:
                return w
        return 0

    def total_weight(self) -> int:
        return sum(w for _, w in self.validators())

    def total_supply(self) -> int:
        return (sum(self.balances.values()) + sum(self.stakes.values())
                + sum(self.unbonding_total(a) for a in self.unbonding))

    # --- transactions ------------------------------------------------------
    def apply_tx(self, tx: Transaction, height: int = 0) -> int:
        """Apply one transaction. Returns the fee paid. Raises InvalidTx without changing state."""
        tx.check_format()
        if not tx.signature_valid():
            raise InvalidTx("bad signature")
        s = tx.sender_address
        if tx.nonce != self.nonce(s):
            raise InvalidTx(f"bad nonce {tx.nonce}, expected {self.nonce(s)}")

        if tx.kind == "unstake":
            if self.balance(s) < tx.fee:
                raise InvalidTx("insufficient balance for fee")
            if self.stake(s) < tx.amount:
                raise InvalidTx("unstake exceeds stake")
            left = self.stake(s) - tx.amount
            if 0 < left < MIN_STAKE:
                raise InvalidTx(f"remaining stake would be below the minimum of {MIN_STAKE}")
            self.balances[s] = self.balance(s) - tx.fee
            if left:
                self.stakes[s] = left
            else:
                del self.stakes[s]
            # Coins are not returned at once: they stay locked, and slashable, while unbonding.
            self.unbonding[s] = self.unbonding.get(s, ()) + ((height + UNBOND_BLOCKS, tx.amount),)
            self._vcache = None
        else:
            cost = tx.amount + tx.fee
            if self.balance(s) < cost:
                raise InvalidTx(f"insufficient balance: has {self.balance(s)}, needs {cost}")
            if tx.kind == "stake":
                if s in self.slashed:
                    raise InvalidTx("slashed validators cannot stake again")
                if self.stake(s) + tx.amount < MIN_STAKE:
                    raise InvalidTx(f"stake must reach the minimum of {MIN_STAKE}")
            self.balances[s] = self.balance(s) - cost
            if tx.kind == "transfer":
                self.balances[tx.to] = self.balance(tx.to) + tx.amount
            else:
                if self.stake(s) < MIN_STAKE:
                    self.active_since[s] = height   # no vote duties for blocks before it joined
                self.stakes[s] = self.stake(s) + tx.amount
                # A newcomer starts with low reputation: influence cannot be bought instantly.
                self.reputation.setdefault(s, REP_NEW)
                self._vcache = None

        self.nonces[s] = tx.nonce + 1
        return tx.fee

    # --- block-level rules -------------------------------------------------
    def release_unbonded(self, height: int) -> None:
        for a in list(self.unbonding):
            keep, free = [], 0
            for rel, amt in self.unbonding[a]:
                if rel <= height:
                    free += amt
                else:
                    keep.append((rel, amt))
            if free:
                self.balances[a] = self.balance(a) + free
            if keep:
                self.unbonding[a] = tuple(keep)
            else:
                del self.unbonding[a]

    def reward(self, a: str, amount: int) -> None:
        self.balances[a] = self.balance(a) + amount

    def record_duty(self, a: str, ok: bool, samples: int = 1) -> None:
        """Add `samples` outcomes of one duty to the validator's reputation average."""
        if a in self.slashed:
            return
        r = self.rep(a)
        for _ in range(samples):
            r = rep_sample(r, ok)
        self.reputation[a] = r
        self._vcache = None

    def slash(self, a: str, height: int) -> int:
        """Cut the offender's stake and unbonding coins. Returns the amount removed."""
        cut = self.stake(a) * SLASH_NUM // SLASH_DEN
        if cut:
            self.stakes[a] = self.stake(a) - cut
        new_unb = []
        for rel, amt in self.unbonding.get(a, ()):
            c = amt * SLASH_NUM // SLASH_DEN
            cut += c
            new_unb.append((rel, amt - c))
        if a in self.unbonding:
            self.unbonding[a] = tuple(new_unb)
        self.reputation[a] = 0
        self.slashed[a] = height
        self._vcache = None
        return cut

    def add_vote(self, target: str, height: int, voter: str) -> bool:
        if voter in self.vote_log.get(height, ()):
            return False
        self.vote_log[height] = self.vote_log.get(height, frozenset()) | {voter}
        if height > self.finalized_height:           # only votes for non-final blocks matter for finality
            h, voters = self.votes.get(target, (height, frozenset()))
            self.votes[target] = (h, voters | {voter})
        return True

    def score_votes(self, due_height: int, duty: list) -> None:
        """Each validator in `duty` had to vote for the block at `due_height`; credit or debit them."""
        if due_height <= 0:
            return
        done = self.vote_log.get(due_height, frozenset())
        for a in duty:
            if self.active_since.get(a, 0) < due_height:     # only blocks after it joined
                self.record_duty(a, a in done)
        for h in [h for h in self.vote_log if h < due_height - VOTE_WINDOW]:
            del self.vote_log[h]

    def update_finality(self) -> bool:
        """Finalize the highest voted block backed by >= 2/3 of current total weight."""
        total = self.total_weight()
        if total == 0:
            return False
        weights = dict(self.validators())
        best = None
        for target, (h, voters) in self.votes.items():
            if h <= self.finalized_height:
                continue
            w = sum(weights.get(v, 0) for v in voters)
            if w * FINALITY_DEN >= total * FINALITY_NUM and (best is None or h > best[0]):
                best = (h, target)
        if best is None:
            return False
        self.finalized_height, self.finalized_hash = best
        self.votes = {t: hv for t, hv in self.votes.items() if hv[0] > self.finalized_height}
        return True

    def to_dict(self) -> dict:
        return {
            "balances": self.balances,
            "nonces": self.nonces,
            "stakes": self.stakes,
            "reputation": self.reputation,
            "unbonding": {a: [list(x) for x in v] for a, v in self.unbonding.items()},
            "slashed": self.slashed,
            "finalized_height": self.finalized_height,
            "finalized_hash": self.finalized_hash,
            "burned": self.burned,
        }
