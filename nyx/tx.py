"""Transactions: transfer, stake and unstake."""
from dataclasses import dataclass

from .crypto import address_from_pubkey, canonical, is_address, is_pubkey, sha256_hex, verify
from .params import MIN_FEE

KINDS = ("transfer", "stake", "unstake")


class InvalidTx(Exception):
    pass


@dataclass
class Transaction:
    kind: str
    sender: str      # sender's public key (hex); the address is derived from it
    to: str          # recipient address; empty for stake/unstake
    amount: int
    fee: int
    nonce: int
    signature: str = ""

    def body(self) -> dict:
        return {
            "kind": self.kind,
            "sender": self.sender,
            "to": self.to,
            "amount": self.amount,
            "fee": self.fee,
            "nonce": self.nonce,
        }

    @property
    def hash(self) -> str:
        return sha256_hex(canonical(self.body()))

    @property
    def sender_address(self) -> str:
        return address_from_pubkey(self.sender)

    def sign(self, kp) -> "Transaction":
        if kp.public_hex != self.sender:
            raise ValueError("key does not match sender")
        self.signature = kp.sign(canonical(self.body()))
        return self

    def signature_valid(self) -> bool:
        return verify(self.sender, canonical(self.body()), self.signature)

    def check_format(self) -> None:
        """Checks that need no chain state."""
        if self.kind not in KINDS:
            raise InvalidTx(f"unknown kind {self.kind!r}")
        if not is_pubkey(self.sender):
            raise InvalidTx("sender must be a 32-byte hex public key")
        for name in ("amount", "fee", "nonce"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool):
                raise InvalidTx(f"{name} must be an integer")
        if self.amount <= 0:
            raise InvalidTx("amount must be positive")
        if self.fee < MIN_FEE:
            raise InvalidTx(f"fee must be at least {MIN_FEE}")
        if self.nonce < 0:
            raise InvalidTx("nonce must be non-negative")
        if self.kind == "transfer" and not is_address(self.to):
            raise InvalidTx("invalid recipient address")
        if self.kind != "transfer" and self.to:
            raise InvalidTx(f"{self.kind} must not have a recipient")

    def to_dict(self) -> dict:
        d = self.body()
        d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        try:
            return cls(
                kind=str(d["kind"]),
                sender=str(d["sender"]),
                to=str(d.get("to", "")),
                amount=d["amount"],
                fee=d["fee"],
                nonce=d["nonce"],
                signature=str(d.get("signature", "")),
            )
        except (KeyError, TypeError, AttributeError) as e:
            raise InvalidTx(f"malformed transaction: {e}")
