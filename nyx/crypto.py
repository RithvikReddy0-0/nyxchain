"""Keys, signatures, hashing and canonical encoding."""
import hashlib
import json
import os
from collections import OrderedDict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ADDRESS_PREFIX = "nyx"
ADDRESS_LEN = len(ADDRESS_PREFIX) + 40


def canonical(obj) -> bytes:
    """Deterministic JSON bytes: same object -> same bytes on every node."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def address_from_pubkey(pub_hex: str) -> str:
    return ADDRESS_PREFIX + sha256_hex(bytes.fromhex(pub_hex))[:40]


def is_address(s) -> bool:
    if not isinstance(s, str) or len(s) != ADDRESS_LEN or not s.startswith(ADDRESS_PREFIX):
        return False
    try:
        int(s[len(ADDRESS_PREFIX):], 16)
        return True
    except ValueError:
        return False


def is_pubkey(s) -> bool:
    if not isinstance(s, str) or len(s) != 64:
        return False
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


class KeyPair:
    """An Ed25519 signing key and the address derived from it."""

    def __init__(self, sk: Ed25519PrivateKey):
        self._sk = sk
        self.public_hex = sk.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()
        self.address = address_from_pubkey(self.public_hex)

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_hex(cls, h: str) -> "KeyPair":
        return cls(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(h)))

    @property
    def private_hex(self) -> str:
        return self._sk.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        ).hex()

    def sign(self, msg: bytes) -> str:
        return self._sk.sign(msg).hex()

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(
                {"address": self.address, "public_key": self.public_hex, "private_key": self.private_hex},
                f,
                indent=2,
            )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str) -> "KeyPair":
        with open(path) as f:
            return cls.from_private_hex(json.load(f)["private_key"])


# Signatures are checked several times (mempool, block packing, block import on
# every node). Remember results so each one is verified only once per process.
_verified = OrderedDict()
_CACHE_MAX = 200_000


def verify(pub_hex: str, msg: bytes, sig_hex: str) -> bool:
    key = (pub_hex, msg, sig_hex)
    hit = _verified.get(key)
    if hit is not None:
        return hit
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(bytes.fromhex(sig_hex), msg)
        ok = True
    except (InvalidSignature, ValueError, TypeError):
        ok = False
    _verified[key] = ok
    if len(_verified) > _CACHE_MAX:
        _verified.popitem(last=False)
    return ok
