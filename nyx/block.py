"""Blocks and Merkle roots.

The header commits to the transactions, finality votes and slashing evidence
through a single Merkle root, so the proposer's signature over the header
covers everything in the block.
"""
from dataclasses import dataclass, field

from .crypto import address_from_pubkey, canonical, sha256_hex, verify
from .tx import InvalidTx, Transaction

ZERO_HASH = "0" * 64


class InvalidBlock(Exception):
    pass


def merkle_root(hashes: list) -> str:
    if not hashes:
        return sha256_hex(b"")
    layer = list(hashes)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256_hex(bytes.fromhex(layer[i] + layer[i + 1])) for i in range(0, len(layer), 2)]
    return layer[0]


def body_root(txs: list, votes: list, evidence: list) -> str:
    return merkle_root(
        [merkle_root([t.hash for t in txs]),
         merkle_root([sha256_hex(canonical(v)) for v in votes]),
         merkle_root([sha256_hex(canonical(e)) for e in evidence])]
    )


@dataclass
class Block:
    height: int
    prev_hash: str
    slot: int
    timestamp_ms: int
    proposer: str            # proposer's public key (hex); empty for genesis
    tx_root: str             # body_root over txs, votes and evidence
    txs: list = field(default_factory=list)
    votes: list = field(default_factory=list)      # vote dicts
    evidence: list = field(default_factory=list)   # evidence dicts
    signature: str = ""

    def header(self) -> dict:
        return {
            "height": self.height,
            "prev_hash": self.prev_hash,
            "slot": self.slot,
            "timestamp_ms": self.timestamp_ms,
            "proposer": self.proposer,
            "tx_root": self.tx_root,
        }

    @property
    def hash(self) -> str:
        return sha256_hex(canonical(self.header()))

    @property
    def proposer_address(self) -> str:
        return address_from_pubkey(self.proposer) if self.proposer else ""

    def sign(self, kp) -> "Block":
        self.signature = kp.sign(canonical(self.header()))
        return self

    def signature_valid(self) -> bool:
        return bool(self.proposer) and verify(self.proposer, canonical(self.header()), self.signature)

    def signed_header(self) -> dict:
        d = self.header()
        d["signature"] = self.signature
        return d

    def to_dict(self) -> dict:
        d = self.signed_header()
        d["txs"] = [t.to_dict() for t in self.txs]
        d["votes"] = list(self.votes)
        d["evidence"] = list(self.evidence)
        return d

    def summary(self) -> dict:
        d = self.header()
        d["hash"] = self.hash
        d["proposer_address"] = self.proposer_address
        d["tx_count"] = len(self.txs)
        d["vote_count"] = len(self.votes)
        d["evidence_count"] = len(self.evidence)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        try:
            votes, evidence = d.get("votes", []), d.get("evidence", [])
            if not isinstance(votes, list) or not isinstance(evidence, list):
                raise TypeError("votes and evidence must be lists")
            return cls(
                height=int(d["height"]),
                prev_hash=str(d["prev_hash"]),
                slot=int(d["slot"]),
                timestamp_ms=int(d["timestamp_ms"]),
                proposer=str(d["proposer"]),
                tx_root=str(d["tx_root"]),
                txs=[Transaction.from_dict(t) for t in d.get("txs", [])],
                votes=votes,
                evidence=evidence,
                signature=str(d.get("signature", "")),
            )
        except (KeyError, TypeError, ValueError, AttributeError, InvalidTx) as e:
            raise InvalidBlock(f"malformed block: {e}")


def make_genesis_block(genesis: dict) -> Block:
    """Genesis commits to the whole genesis config, so different networks never share a chain."""
    return Block(
        height=0,
        prev_hash=ZERO_HASH,
        slot=0,
        timestamp_ms=int(genesis["genesis_time"] * 1000),
        proposer="",
        tx_root=sha256_hex(canonical(genesis)),
    )
