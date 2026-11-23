"""Finality votes and slashing evidence.

A vote is a validator's signed statement "I accept block <hash> at <height>".
Votes are gossiped, then packed into later blocks, so every node counts the
same votes and reaches the same finality decision.

Signing two different blocks at the same height (a double vote), or proposing
two different blocks for the same slot (a double block), is equivocation. Two
such signed messages are proof on their own: anyone can check the signatures.
Proof is packed into a block as evidence, and the offender is slashed.
"""
from dataclasses import dataclass

from .crypto import address_from_pubkey, canonical, is_pubkey, sha256_hex, verify


class InvalidVote(Exception):
    pass


@dataclass
class Vote:
    height: int
    block_hash: str
    validator: str   # public key (hex)
    signature: str = ""

    def message(self) -> bytes:
        return canonical({"type": "vote", "height": self.height, "block": self.block_hash})

    @property
    def address(self) -> str:
        return address_from_pubkey(self.validator)

    @property
    def id(self) -> str:
        return sha256_hex(canonical(self.to_dict()))

    def sign(self, kp) -> "Vote":
        self.signature = kp.sign(self.message())
        return self

    def valid(self) -> bool:
        return (
            isinstance(self.height, int)
            and self.height > 0
            and isinstance(self.block_hash, str)
            and len(self.block_hash) == 64
            and is_pubkey(self.validator)
            and verify(self.validator, self.message(), self.signature)
        )

    def to_dict(self) -> dict:
        return {"height": self.height, "block": self.block_hash, "validator": self.validator, "signature": self.signature}

    @classmethod
    def from_dict(cls, d: dict) -> "Vote":
        try:
            return cls(int(d["height"]), str(d["block"]), str(d["validator"]), str(d.get("signature", "")))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise InvalidVote(f"malformed vote: {e}")


def check_evidence(ev: dict):
    """Validate a piece of evidence. Returns (offender_address, description) or raises InvalidVote."""
    from .block import Block, InvalidBlock  # avoid an import cycle

    if not isinstance(ev, dict):
        raise InvalidVote("evidence must be an object")
    kind = ev.get("kind")
    if kind == "double_vote":
        a, b = Vote.from_dict(ev.get("a", {})), Vote.from_dict(ev.get("b", {}))
        if a.validator != b.validator:
            raise InvalidVote("votes from different validators")
        if a.height != b.height or a.block_hash == b.block_hash:
            raise InvalidVote("votes do not conflict")
        if not (a.valid() and b.valid()):
            raise InvalidVote("bad vote signature")
        return a.address, f"double vote at height {a.height}"
    if kind == "double_block":
        try:
            a, b = Block.from_dict(ev.get("a", {})), Block.from_dict(ev.get("b", {}))
        except InvalidBlock as e:
            raise InvalidVote(str(e))
        if a.proposer != b.proposer or not a.proposer:
            raise InvalidVote("blocks from different proposers")
        if a.slot != b.slot or a.hash == b.hash:
            raise InvalidVote("blocks do not conflict")
        if not (a.signature_valid() and b.signature_valid()):
            raise InvalidVote("bad block signature")
        return a.proposer_address, f"two blocks in slot {a.slot}"
    raise InvalidVote(f"unknown evidence kind {kind!r}")


def evidence_key(ev: dict) -> str:
    return sha256_hex(canonical(ev))
