"""Pending transactions, finality votes and slashing evidence waiting to go into a block."""
from collections import defaultdict

from .tx import InvalidTx, Transaction
from .vote import InvalidVote, Vote, check_evidence, evidence_key

MAX_NONCE_GAP = 64


class Mempool:
    def __init__(self, max_size: int = 20_000):
        self.max_size = max_size
        self.txs = {}
        self.votes = {}       # vote id -> vote dict
        self.evidence = {}    # evidence key -> evidence dict

    def __len__(self):
        return len(self.txs)

    # --- transactions --------------------------------------------------------
    def add(self, tx: Transaction, state) -> bool:
        """Returns True if new, False if already known. Raises InvalidTx."""
        h = tx.hash
        if h in self.txs:
            return False
        tx.check_format()
        if not tx.signature_valid():
            raise InvalidTx("bad signature")
        n = state.nonce(tx.sender_address)
        if tx.nonce < n:
            raise InvalidTx("nonce already used")
        if tx.nonce > n + MAX_NONCE_GAP:
            raise InvalidTx("nonce too far ahead")
        if len(self.txs) >= self.max_size:
            raise InvalidTx("mempool full")
        self.txs[h] = tx
        return True

    def next_nonce(self, address: str, state) -> int:
        n = state.nonce(address)
        pending = {t.nonce for t in self.txs.values() if t.sender_address == address}
        while n in pending:
            n += 1
        return n

    def select(self, state, limit: int, height: int = 0) -> list:
        """Pick transactions that apply cleanly, in nonce order per sender, highest-fee senders first."""
        by_sender = defaultdict(list)
        for t in self.txs.values():
            by_sender[t.sender_address].append(t)
        senders = sorted(by_sender, key=lambda s: (-max(t.fee for t in by_sender[s]), s))
        st, chosen = state.copy(), []
        for s in senders:
            for t in sorted(by_sender[s], key=lambda t: t.nonce):
                if len(chosen) >= limit:
                    return chosen
                try:
                    st.apply_tx(t, height)
                except InvalidTx:
                    break
                chosen.append(t)
        return chosen

    # --- votes and evidence --------------------------------------------------
    def add_vote(self, v: Vote) -> bool:
        if v.id in self.votes:
            return False
        if not v.valid():
            raise InvalidVote("bad vote")
        self.votes[v.id] = v.to_dict()
        return True

    def add_evidence(self, ev: dict) -> bool:
        k = evidence_key(ev)
        if k in self.evidence:
            return False
        check_evidence(ev)
        self.evidence[k] = ev
        return True

    # --- cleanup -------------------------------------------------------------
    def prune(self, state, height: int, window: int) -> None:
        """Drop what is now included or can never be."""
        for h, t in list(self.txs.items()):
            if t.nonce < state.nonce(t.sender_address):
                del self.txs[h]
        for k, d in list(self.votes.items()):
            # Keep counted votes until they leave the window: after a reorg they may need re-including.
            if d["height"] < height - window:
                del self.votes[k]
        for k, ev in list(self.evidence.items()):
            try:
                who, _ = check_evidence(ev)
            except InvalidVote:
                who = None
            if who is None or who in state.slashed:
                del self.evidence[k]
