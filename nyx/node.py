"""A NyxChain node: chain + mempool + P2P + block production + voting + HTTP API + dashboard."""
import asyncio
import json
import os
import sys
import time
from collections import deque
from urllib.parse import parse_qs, urlsplit

from .block import Block, InvalidBlock
from .chain import Chain
from .crypto import KeyPair, is_address
from .mempool import Mempool
from .p2p import P2P
from .params import MAX_TXS_PER_BLOCK, REP_MAX, VOTE_WINDOW
from .tx import InvalidTx, Transaction
from .vote import InvalidVote, Vote, check_evidence

PRODUCE_DELAY_MS = 150   # wait a moment into the slot so the previous block can arrive
STARTUP_GRACE_S = 2.0    # listen for peers before producing, so a restarted node syncs first
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class Node:
    def __init__(self, config: dict):
        self.config = config
        self.node_id = config["node_id"]
        with open(config["genesis_file"]) as f:
            self.genesis = json.load(f)
        self.chain = Chain(self.genesis)
        self.mempool = Mempool()
        self.key = KeyPair.load(config["key_file"]) if config.get("key_file") else None
        host = config.get("host", "127.0.0.1")
        self.api_url = f"http://{host}:{config['api_port']}"
        self.p2p = P2P(self, host, int(config["p2p_port"]), config.get("peers", []))
        self.data_dir = config["data_dir"]
        os.makedirs(self.data_dir, exist_ok=True)
        self.block_log = os.path.join(self.data_dir, "blocks.jsonl")
        self.vote_file = os.path.join(self.data_dir, "last_vote.txt")
        self.byzantine = config.get("byzantine")   # attack mode, for the slashing demo only
        self._attacked = False
        self._last_slot_tried = -1
        self._started = time.monotonic()
        self.events = deque(maxlen=200)
        self.seen_votes = {}     # (validator address, height) -> vote dict
        self.seen_headers = {}   # (proposer address, slot) -> signed header dict
        self.last_voted = 0

    # --- logging -------------------------------------------------------------
    def log(self, tag: str, msg: str):
        t = time.strftime("%H:%M:%S")
        print(f"{t} [{self.node_id} h={self.chain.height} f={self.chain.finalized_height}] {tag:<7} {msg}", flush=True)
        if tag not in ("PEER", "TX", "VOTE"):
            self.events.append({"time": t, "ts": time.time(), "tag": tag, "msg": msg})

    @staticmethod
    def short(a: str) -> str:
        return a[:9] + ".." if a else "-"

    # --- persistence ---------------------------------------------------------
    def load_from_disk(self):
        if os.path.exists(self.vote_file):
            try:
                with open(self.vote_file) as f:
                    self.last_voted = int(f.read().strip() or 0)
            except (OSError, ValueError):
                pass
        if not os.path.exists(self.block_log):
            return
        n = 0
        with open(self.block_log) as f:
            for line in f:
                try:
                    if self.chain.add_block(Block.from_dict(json.loads(line))) not in ("known", "orphan"):
                        n += 1
                except (ValueError, InvalidBlock):
                    continue
        self.last_voted = max(self.last_voted, self.chain.height)
        self.log("DISK", f"restored {n} blocks, tip #{self.chain.height}, finalized #{self.chain.finalized_height}")

    def _persist(self, block: Block):
        with open(self.block_log, "a") as f:
            f.write(json.dumps(block.to_dict()) + "\n")

    def _save_vote_height(self):
        with open(self.vote_file, "w") as f:
            f.write(str(self.last_voted))

    # --- transactions --------------------------------------------------------
    def submit_tx(self, d: dict) -> str:
        tx = Transaction.from_dict(d)
        if self.mempool.add(tx, self.chain.state):
            self.p2p.broadcast({"type": "tx", "tx": tx.to_dict()})
            self.log("TX", f"{tx.kind} {tx.amount} from {self.short(tx.sender_address)} nonce={tx.nonce}")
        return tx.hash

    def receive_tx(self, d: dict):
        try:
            self.submit_tx(d)
        except InvalidTx:
            pass

    # --- votes and evidence --------------------------------------------------
    def receive_vote(self, d: dict, relay: bool = True):
        try:
            v = Vote.from_dict(d)
            if not self.mempool.add_vote(v):
                return
        except InvalidVote:
            return
        key = (v.address, v.height)
        prev = self.seen_votes.get(key)
        if prev is None:
            self.seen_votes[key] = v.to_dict()
        elif prev["block"] != v.block_hash:
            self.add_evidence({"kind": "double_vote", "a": prev, "b": v.to_dict()})
        if relay:
            self.p2p.broadcast({"type": "vote", "vote": v.to_dict()})

    def receive_evidence(self, ev: dict):
        self.add_evidence(ev)

    def add_evidence(self, ev: dict):
        if isinstance(ev, dict) and isinstance(ev.get("a"), dict) and isinstance(ev.get("b"), dict):
            # The same pair seen in either order is one piece of evidence.
            a, b = sorted([ev["a"], ev["b"]], key=lambda x: json.dumps(x, sort_keys=True))
            ev = {"kind": ev.get("kind"), "a": a, "b": b}
        try:
            who, what = check_evidence(ev)
            if who in self.chain.state.slashed or not self.mempool.add_evidence(ev):
                return
        except InvalidVote:
            return
        self.log("CAUGHT", f"{self.short(who)} equivocated ({what}); evidence queued for slashing")
        self.p2p.broadcast({"type": "evidence", "evidence": ev})

    def _check_double_block(self, block: Block):
        if not block.proposer or not block.signature_valid():
            return
        key = (block.proposer_address, block.slot)
        prev = self.seen_headers.get(key)
        hdr = block.signed_header()
        if prev is None:
            self.seen_headers[key] = hdr
        elif Block.from_dict(prev).hash != block.hash:
            self.add_evidence({"kind": "double_block", "a": prev, "b": hdr})

    def _vote_for(self, block: Block) -> dict:
        v = Vote(block.height, block.hash, self.key.public_hex).sign(self.key)
        self.receive_vote(v.to_dict(), relay=False)
        self.p2p.broadcast({"type": "vote", "vote": v.to_dict()})
        return v.to_dict()

    def _maybe_vote(self, block: Block):
        """Honest rule: vote at most once per height, and only for fresh blocks."""
        if not self.key or self.chain.state.weight(self.key.address) == 0:
            return
        if block.height <= self.last_voted or block.slot < self.chain.current_slot() - 2:
            return
        self.last_voted = block.height
        self._save_vote_height()
        self._vote_for(block)

    # --- blocks --------------------------------------------------------------
    async def receive_block(self, d: dict, writer=None, from_sync: bool = False):
        try:
            block = Block.from_dict(d)
        except InvalidBlock:
            return
        self._check_double_block(block)
        try:
            status = self.chain.add_block(block)
        except InvalidBlock as e:
            self.log("REJECT", f"block #{block.height} from {self.short(block.proposer_address)}: {e}")
            return
        if status == "known":
            return
        if status == "orphan":
            if writer is not None:
                await self.p2p.request_sync(writer, self.chain.finalized_height)
            return
        self._accepted(block, status)
        if not from_sync:
            self.p2p.broadcast({"type": "block", "block": block.to_dict()})

    def _accepted(self, block: Block, status: str):
        self._persist(block)
        if status == "side":
            self.log("FORK", f"side block #{block.height} by {self.short(block.proposer_address)} (not on main chain)")
            return
        tag = "REORG" if status == "reorg" else "BLOCK"
        mine = self.key and block.proposer_address == self.key.address
        self.log(tag, f"#{block.height} slot {block.slot} by {'ME' if mine else self.short(block.proposer_address)}"
                      f" txs={len(block.txs)} votes={len(block.votes)} {block.hash[:10]}")
        st, ev = self.chain.state, self.chain.events[block.hash]
        for slot, addr in ev["missed"]:
            self.log("MISSED", f"slot {slot} skipped by {self.short(addr)} -> reputation {st.rep(addr) / 100:.0f}%")
        for who, cut, what in ev["slashed"]:
            self.log("SLASHED", f"{self.short(who)} lost {cut} NYX for {what}; removed from validator set")
        if ev["finalized"]:
            self.log("FINAL", f"block #{ev['finalized'][0]} finalized ({ev['finalized'][1][:10]})")
        self.mempool.prune(st, self.chain.height, VOTE_WINDOW)
        self._maybe_vote(block)

    # --- block production ----------------------------------------------------
    def _assemble(self, slot: int) -> Block:
        evidence = self.chain.pick_evidence(list(self.mempool.evidence.values()))
        offenders = set()
        for ev in evidence:
            offenders.add(check_evidence(ev)[0])
        votes = self.chain.pick_votes(list(self.mempool.votes.values()), exclude=offenders)
        txs = self.mempool.select(self.chain.state, MAX_TXS_PER_BLOCK, self.chain.height + 1)
        return self.chain.build_block(self.key, slot, txs, votes, evidence)

    async def produce_loop(self):
        while True:
            await asyncio.sleep(0.05)
            if not self.key or time.monotonic() - self._started < STARTUP_GRACE_S:
                continue
            if self.p2p.best_peer_height() > self.chain.height + 1:
                continue  # still catching up
            slot = self.chain.current_slot()
            if slot <= self._last_slot_tried or slot <= self.chain.tip_block.slot:
                continue
            if self.chain.clock() < self.chain.slot_start(slot) + PRODUCE_DELAY_MS:
                continue
            self._last_slot_tried = slot
            if self.chain.proposer_for(slot) != self.key.address:
                continue
            if self._should_attack():
                self._equivocate(slot)
                continue
            block = self._assemble(slot)
            try:
                status = self.chain.add_block(block)
            except InvalidBlock as e:
                self.log("ERROR", f"own block invalid ({e}); retrying without transactions")
                block = self.chain.build_block(self.key, slot, [], block.votes, block.evidence)
                try:
                    status = self.chain.add_block(block)
                except InvalidBlock as e2:
                    self.log("ERROR", f"own block invalid: {e2}")
                    continue
            self.seen_headers[(self.key.address, slot)] = block.signed_header()
            self._accepted(block, status)
            self.p2p.broadcast({"type": "block", "block": block.to_dict()})

    # --- attack mode (demo only) -------------------------------------------------
    def _should_attack(self) -> bool:
        return bool(self.byzantine) and not self._attacked and self.chain.height >= int(self.byzantine.get("after_height", 5))

    def _equivocate(self, slot: int):
        """Sign two different blocks for the same slot, send one to each half of the network, and vote for both."""
        self._attacked = True
        a = self._assemble(slot)
        b = self.chain.build_block(self.key, slot, [], a.votes, a.evidence, timestamp_ms=a.timestamp_ms + 1)
        peers = sorted(self.p2p.outbound)
        half = set(peers[: max(1, len(peers) // 2)])
        self.log("ATTACK", f"signing two blocks for slot {slot}: {a.hash[:10]} -> {sorted(half)}, "
                           f"{b.hash[:10]} -> the rest; voting for both")
        self.p2p.broadcast({"type": "block", "block": a.to_dict()}, only=half)
        self.p2p.broadcast({"type": "block", "block": b.to_dict()}, only=set(peers) - half)
        for blk in (a, b):
            v = Vote(blk.height, blk.hash, self.key.public_hex).sign(self.key)
            self.p2p.broadcast({"type": "vote", "vote": v.to_dict()})
        try:
            self._accepted(a, self.chain.add_block(a))
        except InvalidBlock:
            pass

    # --- HTTP API ------------------------------------------------------------
    def status(self) -> dict:
        st, tip = self.chain.state, self.chain.tip_block
        total = st.total_weight() or 1
        return {
            "node_id": self.node_id,
            "chain_id": self.genesis.get("chain_id"),
            "api": self.api_url,
            "height": tip.height,
            "tip": tip.hash,
            "finalized_height": st.finalized_height,
            "finalized_hash": st.finalized_hash,
            "slot": self.chain.current_slot(),
            "slot_seconds": self.chain.slot_ms / 1000,
            "genesis_time": self.chain.genesis_ms / 1000,
            "validator": self.key.address if self.key else None,
            "peers": sorted(self.p2p.peer_id.get(a, a) for a in self.p2p.outbound),
            "peer_apis": sorted(set(self.p2p.peer_api.values())),
            "mempool": len(self.mempool),
            "pending_votes": len(self.mempool.votes),
            "pending_evidence": len(self.mempool.evidence),
            "total_supply": st.total_supply(),
            "burned": st.burned,
            "validators": [
                {"address": a, "stake": st.stake(a), "reputation_pct": st.rep(a) * 100 / REP_MAX,
                 "weight": w, "raw_weight": st.raw_weight(a), "capped": st.raw_weight(a) > w,
                 "share_pct": round(w * 100 / total, 2)}
                for a, w in st.validators()
            ],
            "slashed": [{"address": a, "height": h, "stake": st.stake(a)} for a, h in st.slashed.items()],
            "unbonding": {a: [list(x) for x in v] for a, v in st.unbonding.items()},
        }

    def account(self, addr: str) -> dict:
        st = self.chain.state
        return {
            "address": addr,
            "balance": st.balance(addr),
            "nonce": st.nonce(addr),
            "next_nonce": self.mempool.next_nonce(addr, st),
            "stake": st.stake(addr),
            "unbonding": [list(x) for x in st.unbonding.get(addr, ())],
            "reputation_pct": st.rep(addr) * 100 / REP_MAX if (st.stake(addr) or addr in st.slashed) else None,
            "slashed": addr in st.slashed,
        }

    def block_detail(self, b: Block) -> dict:
        d = b.to_dict()
        d["hash"] = b.hash
        d["proposer_address"] = b.proposer_address
        d["finalized"] = b.height <= self.chain.finalized_height
        ev = self.chain.events.get(b.hash) or {}
        d["events"] = {
            "missed": ev.get("missed", []),
            "slashed": ev.get("slashed", []),
            "finalized": ev.get("finalized"),
        }
        return d

    def route(self, method: str, target: str, body: bytes):
        url = urlsplit(target)
        parts = [p for p in url.path.split("/") if p]
        q = parse_qs(url.query)
        if method == "GET" and parts in ([], ["dashboard"]):
            with open(os.path.join(STATIC_DIR, "dashboard.html"), "rb") as f:
                return 200, f.read()
        if method == "GET" and parts == ["status"]:
            return 200, self.status()
        if method == "GET" and parts == ["events"]:
            return 200, list(self.events)
        if method == "GET" and len(parts) == 2 and parts[0] == "account":
            if not is_address(parts[1]):
                return 400, {"error": "invalid address"}
            return 200, self.account(parts[1])
        if method == "GET" and parts == ["blocks"]:
            start = int(q.get("from", [max(0, self.chain.height - 19)])[0])
            limit = min(200, int(q.get("limit", [20])[0]))
            fin = self.chain.finalized_height
            out = []
            for b in self.chain.canonical_range(start, limit):
                s = b.summary()
                s["finalized"] = b.height <= fin
                out.append(s)
            return 200, out
        if method == "GET" and len(parts) == 2 and parts[0] == "block":
            found = self.chain.canonical_range(int(parts[1]), 1)
            return (200, self.block_detail(found[0])) if found else (404, {"error": "no such block"})
        if method == "POST" and parts == ["tx"]:
            try:
                return 200, {"hash": self.submit_tx(json.loads(body))}
            except (InvalidTx, ValueError) as e:
                return 400, {"error": str(e)}
        if method == "POST" and parts == ["txs"]:   # batch submit, used by the benchmark
            ok, errors = 0, 0
            for d in json.loads(body):
                try:
                    self.submit_tx(d)
                    ok += 1
                except (InvalidTx, ValueError):
                    errors += 1
            return 200, {"accepted": ok, "rejected": errors}
        return 404, {"error": "not found"}

    async def _http(self, reader, writer):
        ctype = "application/json"
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            method, target, _ = line.decode().split(" ", 2)
            headers = {}
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            n = int(headers.get("content-length", 0))
            if n > 20_000_000:
                raise ValueError("body too large")
            body = await reader.readexactly(n) if n else b""
            if method == "OPTIONS":
                code, payload = 200, {}
            else:
                code, payload = self.route(method, target, body)
        except Exception as e:  # never let one bad request kill the node
            code, payload = 400, {"error": str(e)}
        if isinstance(payload, bytes):
            data, ctype = payload, "text/html; charset=utf-8"
        else:
            data = json.dumps(payload, indent=2).encode()
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(code, "OK")
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\n"
            f"Access-Control-Allow-Origin: *\r\nAccess-Control-Allow-Headers: Content-Type\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
        )
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    # --- main ----------------------------------------------------------------
    async def run(self):
        self.load_from_disk()
        await self.p2p.start()
        host = self.config.get("host", "127.0.0.1")
        await asyncio.start_server(self._http, host, int(self.config["api_port"]))
        role = f"validator {self.key.address}" if self.key else "full node"
        if self.byzantine:
            role += " (BYZANTINE attack mode)"
        self.log("START", f"{role} | p2p {self.p2p.listen_addr} | dashboard {self.api_url}/")
        await self.produce_loop()


def run_node(config_path: str):
    with open(config_path) as f:
        config = json.load(f)
    base = os.path.dirname(os.path.abspath(config_path))
    for k in ("genesis_file", "key_file", "data_dir"):
        if config.get(k) and not os.path.isabs(config[k]):
            config[k] = os.path.join(base, config[k])
    try:
        asyncio.run(Node(config).run())
    except KeyboardInterrupt:
        sys.exit(0)
