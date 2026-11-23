"""Peer-to-peer layer: TCP connections carrying newline-delimited JSON messages.

Each node dials every peer it knows about (outbound) and accepts connections
from others (inbound). Gossip goes out over outbound connections; requests are
answered on the connection they arrived on. Peers learn about each other from
the address lists carried in HELLO messages, so a new node only needs one seed.
A peer that goes down is redialled every few seconds, so it rejoins on restart.

Messages:
  hello      {node_id, listen, genesis, height, tip, finalized, peers}
  tx         {tx}
  block      {block}
  vote       {vote}
  evidence   {evidence}
  get_blocks {from}
  blocks     {blocks}
"""
import asyncio
import json
import time

LINE_LIMIT = 32 * 1024 * 1024
DIAL_INTERVAL = 2.0
STATUS_INTERVAL = 4.0
SYNC_BATCH = 500
SYNC_COOLDOWN = 1.5


class P2P:
    def __init__(self, node, host: str, port: int, seeds: list):
        self.node = node
        self.host = host
        self.port = port
        self.listen_addr = f"{host}:{port}"
        self.known = {s for s in seeds if s != self.listen_addr}
        self.outbound = {}        # addr -> StreamWriter
        self.peer_height = {}     # addr -> last height reported
        self.peer_id = {}         # addr -> node_id
        self.peer_api = {}        # addr -> HTTP API URL, for the dashboard
        self._dialing = set()
        self._last_sync = {}

    # --- lifecycle -----------------------------------------------------------
    async def start(self):
        self.server = await asyncio.start_server(self._on_inbound, self.host, self.port, limit=LINE_LIMIT)
        asyncio.create_task(self._dial_loop())
        asyncio.create_task(self._status_loop())

    async def _dial_loop(self):
        while True:
            for addr in list(self.known):
                if addr not in self.outbound and addr not in self._dialing:
                    self._dialing.add(addr)
                    asyncio.create_task(self._dial(addr))
            await asyncio.sleep(DIAL_INTERVAL)

    async def _status_loop(self):
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            self.broadcast(self._hello())

    async def _dial(self, addr: str):
        try:
            host, port = addr.rsplit(":", 1)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, int(port), limit=LINE_LIMIT), timeout=2
            )
        except (OSError, ValueError, asyncio.TimeoutError):
            return
        finally:
            self._dialing.discard(addr)
        self.outbound[addr] = writer
        self.node.log("PEER", f"connected to {addr}")
        await self._send(writer, self._hello())
        await self._read_loop(reader, writer)
        if self.outbound.get(addr) is writer:
            del self.outbound[addr]
            self.peer_height.pop(addr, None)
            self.node.log("PEER", f"lost {self.peer_id.get(addr, addr)} ({addr})")

    async def _on_inbound(self, reader, writer):
        await self._send(writer, self._hello())
        await self._read_loop(reader, writer)

    async def _read_loop(self, reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    try:
                        await self._handle(msg, writer)
                    except (TypeError, ValueError, KeyError, AttributeError):
                        continue  # one malformed message never drops the connection
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, OSError):
            pass
        finally:
            writer.close()

    # --- messages ------------------------------------------------------------
    def _hello(self) -> dict:
        chain = self.node.chain
        return {
            "type": "hello",
            "node_id": self.node.node_id,
            "listen": self.listen_addr,
            "genesis": chain.genesis_hash,
            "height": chain.height,
            "tip": chain.tip,
            "finalized": chain.finalized_height,
            "api": self.node.api_url,
            "peers": sorted(self.known),
        }

    async def _handle(self, msg: dict, writer):
        t = msg.get("type")
        if t == "hello":
            if msg.get("genesis") != self.node.chain.genesis_hash:
                self.node.log("PEER", f"rejecting {msg.get('listen')}: different genesis")
                writer.close()
                return
            listen = str(msg.get("listen", ""))
            if listen and listen != self.listen_addr:
                self.known.add(listen)
                self.peer_id[listen] = str(msg.get("node_id", listen))
                self.peer_height[listen] = int(msg.get("height", 0))
                if isinstance(msg.get("api"), str):
                    self.peer_api[listen] = msg["api"][:100]
            for p in list(msg.get("peers", []))[:64]:
                if isinstance(p, str) and p != self.listen_addr:
                    self.known.add(p)
            if int(msg.get("height", 0)) > self.node.chain.height:
                await self.request_sync(writer, self.node.chain.finalized_height)
        elif t == "tx":
            self.node.receive_tx(msg.get("tx", {}))
        elif t == "block":
            await self.node.receive_block(msg.get("block", {}), writer)
        elif t == "vote":
            self.node.receive_vote(msg.get("vote", {}))
        elif t == "evidence":
            self.node.receive_evidence(msg.get("evidence", {}))
        elif t == "get_blocks":
            start = max(0, int(msg.get("from", 0)))
            blocks = self.node.chain.canonical_range(start, SYNC_BATCH)
            await self._send(writer, {"type": "blocks", "blocks": [b.to_dict() for b in blocks]})
        elif t == "blocks":
            for d in list(msg.get("blocks", [])):
                await self.node.receive_block(d, writer, from_sync=True)

    async def request_sync(self, writer, from_height: int):
        key = id(writer)
        now = time.monotonic()
        if now - self._last_sync.get(key, 0) < SYNC_COOLDOWN:
            return
        self._last_sync[key] = now
        await self._send(writer, {"type": "get_blocks", "from": max(1, from_height)})

    async def _send(self, writer, msg: dict):
        try:
            writer.write(json.dumps(msg).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, RuntimeError, OSError):
            pass

    def broadcast(self, msg: dict, only=None):
        """Send to every outbound peer, or only to the peer addresses in `only`."""
        for addr, w in list(self.outbound.items()):
            if only is None or addr in only:
                asyncio.create_task(self._send(w, msg))

    def best_peer_height(self) -> int:
        return max((self.peer_height.get(a, 0) for a in self.outbound), default=0)
