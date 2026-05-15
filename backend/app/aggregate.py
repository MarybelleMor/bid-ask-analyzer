"""Cross-symbol BID/ASK aggregation feed (``TOTAL`` / ``TOTAL2`` style).

Subscribes to a basket of :class:`~app.feed.SymbolFeed` instances, caches the
most recent sample from each, and once a second emits a combined sample whose
``bid`` / ``ask`` / ``diff`` values are simple per-level sums across the
basket. Each constituent is computed against its own ``mid``, so the sum
represents aggregate USD-denominated depth within ``P %`` of each market.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.feed import MAX_SAMPLES, SAMPLE_INTERVAL, SymbolFeed
from app.history import HistoryBuffer
from app.levels import LEVEL_KEYS, compute_diffs

log = logging.getLogger(__name__)

# Window for treating a constituent's last sample as still valid when summing.
STALE_AFTER_MS = 5_000

Listener = Callable[[dict[str, Any]], Awaitable[None]]


class AggregateFeed:
    """Sums BID/ASK levels across a basket of :class:`SymbolFeed`s."""

    def __init__(self, name: str, basket: dict[str, SymbolFeed]) -> None:
        self.name = name.upper()
        self.basket = basket
        self.history = HistoryBuffer(MAX_SAMPLES)
        self._listeners: set[Listener] = set()
        self._latest: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None

    async def _on_symbol_sample(self, sample: dict[str, Any]) -> None:
        self._latest[sample["symbol"]] = sample

    async def start(self) -> None:
        if self._task is not None:
            return
        for feed in self.basket.values():
            feed.add_listener(self._on_symbol_sample)
        self._task = asyncio.create_task(self._loop(), name=f"agg-{self.name}")

    async def stop(self) -> None:
        for feed in self.basket.values():
            feed.remove_listener(self._on_symbol_sample)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def add_listener(self, listener: Listener) -> None:
        self._listeners.add(listener)

    def remove_listener(self, listener: Listener) -> None:
        self._listeners.discard(listener)

    @property
    def ready(self) -> bool:
        return bool(self._latest)

    def status(self) -> dict[str, Any]:
        return {
            "symbol": self.name,
            "type": "aggregate",
            "ready": self.ready,
            "constituents": sorted(self._latest.keys()),
            "basket_size": len(self.basket),
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
                next_t = time.monotonic()
            try:
                sample = self._compute_sample()
            except Exception:
                log.exception("[%s] aggregate sample failed", self.name)
                continue
            if sample is None:
                continue
            self.history.append(sample)
            await self._broadcast(sample)

    def _compute_sample(self) -> dict[str, Any] | None:
        if not self._latest:
            return None
        now_ms = int(time.time() * 1000)
        recent: list[dict[str, Any]] = [
            smp for smp in self._latest.values() if now_ms - smp["t"] <= STALE_AFTER_MS
        ]
        if not recent:
            return None
        bid: dict[str, float] = dict.fromkeys(LEVEL_KEYS, 0.0)
        ask: dict[str, float] = dict.fromkeys(LEVEL_KEYS, 0.0)
        for smp in recent:
            for key in LEVEL_KEYS:
                bid[key] += float(smp["bid"].get(key, 0.0))
                ask[key] += float(smp["ask"].get(key, 0.0))
        diff = compute_diffs(bid, ask)
        # No single mid makes sense for an aggregate. We surface BTC's mid as
        # a reference when available so the frontend can still draw a meaningful
        # price indicator if it wants one.
        btc = self._latest.get("BTCUSDT")
        mid = float(btc["mid"]) if btc is not None else 0.0
        return {
            "t": now_ms,
            "symbol": self.name,
            "mid": mid,
            "best_bid": 0.0,
            "best_ask": 0.0,
            "bid": bid,
            "ask": ask,
            "diff": diff,
            "constituents": sorted(s["symbol"] for s in recent),
        }

    async def _broadcast(self, sample: dict[str, Any]) -> None:
        if not self._listeners:
            return
        dead: list[Listener] = []
        for listener in list(self._listeners):
            try:
                await listener(sample)
            except Exception:
                log.exception("listener for %s failed", self.name)
                dead.append(listener)
        for listener in dead:
            self._listeners.discard(listener)
