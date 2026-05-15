"""Local Binance spot order-book maintainer.

Implements the algorithm documented at
https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams#how-to-manage-a-local-order-book-correctly:

1. Open WebSocket to ``<symbol>@depth@100ms`` and buffer diff events.
2. Fetch a REST snapshot from ``/api/v3/depth?limit=5000``.
3. Drop buffered events whose ``u`` is < snapshot lastUpdateId.
4. The first event applied must satisfy U <= lastUpdateId+1 <= u.
5. From then on, each event's ``pu``/``U`` must chain to the previous ``u``.

When the chain breaks we drop the book and re-sync.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from typing import Any

import httpx
import websockets
from sortedcontainers import SortedDict

log = logging.getLogger(__name__)


REST_BASE = "https://data-api.binance.vision"
WS_BASE = "wss://data-stream.binance.vision"
DEPTH_LIMIT = 5000
# How often to re-fetch the REST snapshot and merge any newly visible price
# levels back into the live book. Public /api/v3/depth?limit=5000 only returns
# the 5000 best price levels at the moment of the call; levels that sit deeper
# in the book and never change are invisible through the diff stream. By
# repeatedly snapshotting we gradually accumulate those previously hidden
# levels, which gives a deeper aggregated picture for the wider P windows.
RESNAPSHOT_INTERVAL_SEC = float(os.environ.get("RESNAPSHOT_INTERVAL_SEC", "60"))

# Global semaphore that ensures only one maintainer at a time hits the Binance
# REST API for a depth snapshot. This prevents 20 concurrent feeds from
# bursting Binance's rate limits during startup or periodic refreshes.
_snapshot_semaphore = asyncio.Semaphore(1)


class OrderBook:
    """A sorted local copy of Binance spot order book.

    ``bids`` is keyed by negative price so iteration returns highest first.
    ``asks`` is keyed by positive price so iteration returns lowest first.
    """

    __slots__ = ("bids", "asks", "last_update_id", "ready")

    def __init__(self) -> None:
        self.bids: SortedDict[float, float] = SortedDict()
        self.asks: SortedDict[float, float] = SortedDict()
        self.last_update_id: int = 0
        self.ready: bool = False

    def best_bid(self) -> float | None:
        if not self.bids:
            return None
        return -self.bids.keys()[0]

    def best_ask(self) -> float | None:
        if not self.asks:
            return None
        return self.asks.keys()[0]

    def apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.bids.clear()
        self.asks.clear()
        for px, qty in snapshot["bids"]:
            px_f = float(px)
            qty_f = float(qty)
            if qty_f > 0:
                self.bids[-px_f] = qty_f
        for px, qty in snapshot["asks"]:
            px_f = float(px)
            qty_f = float(qty)
            if qty_f > 0:
                self.asks[px_f] = qty_f
        self.last_update_id = int(snapshot["lastUpdateId"])
        self.ready = True

    def apply_diff(self, bids: list[list[str]], asks: list[list[str]]) -> None:
        for px, qty in bids:
            px_f = float(px)
            qty_f = float(qty)
            key = -px_f
            if qty_f == 0:
                self.bids.pop(key, None)
            else:
                self.bids[key] = qty_f
        for px, qty in asks:
            px_f = float(px)
            qty_f = float(qty)
            if qty_f == 0:
                self.asks.pop(px_f, None)
            else:
                self.asks[px_f] = qty_f

    def sum_bid_value(self, low_price: float) -> float:
        """Σ price*quantity for bids with price >= low_price."""
        total = 0.0
        # bids keys are negative; iterating ascending returns highest price first
        for neg_px, qty in self.bids.items():
            px = -neg_px
            if px < low_price:
                break
            total += px * qty
        return total

    def sum_ask_value(self, high_price: float) -> float:
        """Σ price*quantity for asks with price <= high_price."""
        total = 0.0
        for px, qty in self.asks.items():
            if px > high_price:
                break
            total += px * qty
        return total


class OrderBookMaintainer:
    """Keeps an :class:`OrderBook` in sync with Binance for a single symbol."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self.book = OrderBook()
        self._reconnects = 0
        # While a periodic re-snapshot fetch is in flight we record every
        # price touched by an incoming diff so the merge step doesn't put
        # stale snapshot data over a level we already updated.
        self._touched: tuple[set[float], set[float]] | None = None

    async def run(self) -> None:
        """Run forever, reconnecting on any error."""
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] order-book maintainer crashed; reconnecting", self.symbol)
                self.book.ready = False
                self._reconnects += 1
                await asyncio.sleep(min(30, 2 + self._reconnects))

    async def _run_once(self) -> None:
        ws_url = f"{WS_BASE}/ws/{self.symbol.lower()}@depth@100ms"
        log.info("[%s] connecting to %s", self.symbol, ws_url)
        async with websockets.connect(
            ws_url,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**22,
        ) as ws:
            ok = await self._initial_sync(ws)
            if not ok:
                return
            periodic = asyncio.create_task(self._periodic_resnapshot_loop())
            try:
                while True:
                    msg = await ws.recv()
                    event = json.loads(msg)
                    self._handle_event(event)
            finally:
                periodic.cancel()
                try:
                    await periodic
                except (asyncio.CancelledError, Exception):
                    pass

    async def _initial_sync(self, ws: websockets.WebSocketClientProtocol) -> bool:
        """Replicate Binance's initial sync handshake.

        Returns True on success, False to trigger a reconnect via the outer loop.
        """
        buffered: list[dict[str, Any]] = []
        snapshot_task = asyncio.create_task(self._fetch_snapshot())
        try:
            while True:
                msg = await ws.recv()
                event = json.loads(msg)
                if not snapshot_task.done():
                    buffered.append(event)
                    continue
                snapshot = snapshot_task.result()
                self.book.apply_snapshot(snapshot)
                log.info(
                    "[%s] snapshot applied, lastUpdateId=%d, %d bids / %d asks",
                    self.symbol,
                    self.book.last_update_id,
                    len(self.book.bids),
                    len(self.book.asks),
                )
                buffered.append(event)
                started = False
                for buffered_event in buffered:
                    u = int(buffered_event["u"])
                    big_u = int(buffered_event["U"])
                    if u <= self.book.last_update_id:
                        continue
                    if not started:
                        if not (big_u <= self.book.last_update_id + 1 <= u):
                            log.warning(
                                "[%s] first event mismatch (U=%d, lastUpdateId+1=%d, u=%d); resyncing",
                                self.symbol,
                                big_u,
                                self.book.last_update_id + 1,
                                u,
                            )
                            self.book.ready = False
                            return False
                        started = True
                    self.book.apply_diff(buffered_event["b"], buffered_event["a"])
                    self.book.last_update_id = u
                return True
        finally:
            if not snapshot_task.done():
                snapshot_task.cancel()

    async def _periodic_resnapshot_loop(self) -> None:
        """Periodically pull a fresh REST snapshot and merge in unseen levels.

        The diff stream from ``@depth@100ms`` only ever carries level updates,
        so price levels that were below the initial top-5000 (and never moved)
        stay invisible forever. Replaying ``/api/v3/depth?limit=5000`` every
        minute lets us pick them up gradually whenever they enter the top-5000.
        """
        # Stagger the first cycle across the configured interval so that when
        # we run a basket of dozens of feeds their REST snapshot fetches are
        # spread out over the window instead of all hitting Binance in a burst.
        await asyncio.sleep(random.uniform(0, RESNAPSHOT_INTERVAL_SEC))
        while True:
            await asyncio.sleep(RESNAPSHOT_INTERVAL_SEC)
            if not self.book.ready:
                continue
            try:
                self._touched = (set(), set())
                try:
                    snapshot = await self._fetch_snapshot()
                except Exception:
                    log.warning("[%s] periodic snapshot fetch failed", self.symbol, exc_info=True)
                    continue
                touched_bids, touched_asks = self._touched
            finally:
                self._touched = None
            added_b = 0
            for px, qty in snapshot.get("bids", []):
                px_f = float(px)
                qty_f = float(qty)
                if qty_f <= 0:
                    continue
                if px_f in touched_bids:
                    continue
                if -px_f in self.book.bids:
                    continue
                self.book.bids[-px_f] = qty_f
                added_b += 1
            added_a = 0
            for px, qty in snapshot.get("asks", []):
                px_f = float(px)
                qty_f = float(qty)
                if qty_f <= 0:
                    continue
                if px_f in touched_asks:
                    continue
                if px_f in self.book.asks:
                    continue
                self.book.asks[px_f] = qty_f
                added_a += 1
            if added_b or added_a:
                log.info(
                    "[%s] periodic merge: +%d bids, +%d asks (total %d/%d)",
                    self.symbol,
                    added_b,
                    added_a,
                    len(self.book.bids),
                    len(self.book.asks),
                )

    async def _fetch_snapshot(self) -> dict[str, Any]:
        url = f"{REST_BASE}/api/v3/depth"
        params = {"symbol": self.symbol, "limit": DEPTH_LIMIT}
        async with _snapshot_semaphore:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for attempt in range(5):
                    response = await client.get(url, params=params)
                    if response.status_code in (418, 429):
                        wait = min(2 ** (attempt + 1), 60)
                        log.warning(
                            "[%s] snapshot fetch got %d; backing off %ds",
                            self.symbol, response.status_code, wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    response.raise_for_status()
                    return response.json()
                response.raise_for_status()
                return response.json()  # unreachable but keeps mypy happy

    def _handle_event(self, event: dict[str, Any]) -> None:
        u = int(event["u"])
        big_u = int(event["U"])
        if u <= self.book.last_update_id:
            return
        expected = self.book.last_update_id + 1
        if big_u > expected:
            log.warning(
                "[%s] gap in stream (U=%d, expected=%d); resyncing",
                self.symbol,
                big_u,
                expected,
            )
            self.book.ready = False
            raise RuntimeError("order book gap")
        self.book.apply_diff(event["b"], event["a"])
        self.book.last_update_id = u
        if self._touched is not None:
            tbids, tasks = self._touched
            for px, _ in event["b"]:
                tbids.add(float(px))
            for px, _ in event["a"]:
                tasks.add(float(px))
