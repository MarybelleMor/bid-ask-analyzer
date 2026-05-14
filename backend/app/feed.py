"""Per-symbol aggregation feed.

Runs an :class:`~app.orderbook.OrderBookMaintainer` and, once a second,
computes a sample of BID/ASK/DIFF indicators that is broadcast to listeners.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.history import HistoryBuffer
from app.levels import LEVELS, compute_diffs, fmt
from app.orderbook import OrderBookMaintainer

log = logging.getLogger(__name__)

SAMPLE_INTERVAL = 1.0  # seconds
HISTORY_HOURS = 24
MAX_SAMPLES = int(HISTORY_HOURS * 3600 / SAMPLE_INTERVAL)

Listener = Callable[[dict[str, Any]], Awaitable[None]]


class SymbolFeed:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self.maintainer = OrderBookMaintainer(self.symbol)
        self.history = HistoryBuffer(MAX_SAMPLES)
        self._listeners: set[Listener] = set()
        self._task: asyncio.Task[None] | None = None
        self._ob_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._ob_task = asyncio.create_task(self.maintainer.run(), name=f"ob-{self.symbol}")
        self._task = asyncio.create_task(self._loop(), name=f"feed-{self.symbol}")

    async def stop(self) -> None:
        for task in (self._task, self._ob_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None
        self._ob_task = None

    def add_listener(self, listener: Listener) -> None:
        self._listeners.add(listener)

    def remove_listener(self, listener: Listener) -> None:
        self._listeners.discard(listener)

    @property
    def ready(self) -> bool:
        return self.maintainer.book.ready

    @property
    def book_size(self) -> int:
        book = self.maintainer.book
        return len(book.bids) + len(book.asks)

    def status(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ready": self.ready,
            "bids": len(self.maintainer.book.bids),
            "asks": len(self.maintainer.book.asks),
            "last_update_id": self.maintainer.book.last_update_id,
            "samples": len(self.history),
        }

    async def _loop(self) -> None:
        next_t = time.monotonic()
        while True:
            next_t += SAMPLE_INTERVAL
            sleep_for = next_t - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            else:
                # we're behind; resync the schedule
                next_t = time.monotonic()
            try:
                sample = self._compute_sample()
            except Exception:
                log.exception("[%s] sample failed", self.symbol)
                continue
            if sample is None:
                continue
            self.history.append(sample)
            await self._broadcast(sample)

    def _compute_sample(self) -> dict[str, Any] | None:
        book = self.maintainer.book
        if not book.ready:
            return None
        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if best_bid is None or best_ask is None:
            return None
        mid = (best_bid + best_ask) / 2.0
        bid: dict[str, float] = {}
        ask: dict[str, float] = {}
        for level in LEVELS:
            low = mid * (1.0 - level / 100.0)
            high = mid * (1.0 + level / 100.0)
            key = fmt(level)
            bid[key] = book.sum_bid_value(low)
            ask[key] = book.sum_ask_value(high)
        diff = compute_diffs(bid, ask)
        return {
            "t": int(time.time() * 1000),
            "symbol": self.symbol,
            "mid": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid": bid,
            "ask": ask,
            "diff": diff,
        }

    async def _broadcast(self, sample: dict[str, Any]) -> None:
        if not self._listeners:
            return
        dead: list[Listener] = []
        for listener in list(self._listeners):
            try:
                await listener(sample)
            except Exception:
                log.exception("listener for %s failed", self.symbol)
                dead.append(listener)
        for listener in dead:
            self._listeners.discard(listener)
