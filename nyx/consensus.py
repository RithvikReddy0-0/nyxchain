"""Hybrid stake x reputation proposer selection.

Time is divided into fixed slots. For each slot, exactly one validator is
eligible to propose, chosen by a deterministic weighted lottery seeded by the
parent block hash and the slot number. A validator's chance is proportional
to its voting weight: stake * reputation, after the per-validator cap.

If the chosen validator is offline, the slot stays empty and the next slot
picks again. When the next block arrives, every node can see which slots were
skipped and who was supposed to fill them, and records failed duties against
their reputation. Dead validators therefore lose weight block by block and the
network stops waiting on them: this is how the chain self-heals.
"""
from .crypto import sha256_hex
from .params import MAX_MISSED_SCAN


def current_slot(now_ms: int, genesis_ms: int, slot_ms: int) -> int:
    return max(0, (now_ms - genesis_ms) // slot_ms)


def slot_start_ms(slot: int, genesis_ms: int, slot_ms: int) -> int:
    return genesis_ms + slot * slot_ms


def select_proposer(state, parent_hash: str, slot: int):
    validators = state.validators()
    total = sum(w for _, w in validators)
    if total == 0:
        return None
    r = int(sha256_hex(f"{parent_hash}:{slot}".encode()), 16) % total
    for addr, w in validators:
        if r < w:
            return addr
        r -= w
    return validators[-1][0]  # unreachable


def missed_proposers(state, parent_hash: str, parent_slot: int, slot: int) -> list:
    """(slot, address) for every slot skipped between the parent and this block."""
    start = max(parent_slot + 1, slot - MAX_MISSED_SCAN)
    out = []
    for s in range(start, slot):
        a = select_proposer(state, parent_hash, s)
        if a:
            out.append((s, a))
    return out
