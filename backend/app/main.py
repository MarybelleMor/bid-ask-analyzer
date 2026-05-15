"""FastAPI entrypoint for BID/ASK Analyzer."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.aggregate import AggregateFeed
from app.feed import SAMPLE_INTERVAL, SymbolFeed
from app.levels import DIFF_NAMES, LEVEL_KEYS
from app.orderbook import REST_BASE

log = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ["BTCUSDT"]
# Default basket for the synthetic ``TOTAL`` coin type. Sum of BID/ASK depth
# across these markets, each computed against its own mid, becomes the
# aggregate liquidity number we expose under symbol ``TOTAL``.
DEFAULT_TOTAL_BASKET = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "TRXUSDT", "LINKUSDT",
    "DOTUSDT", "POLUSDT", "NEARUSDT", "LTCUSDT", "ATOMUSDT",
    "ICPUSDT", "ETCUSDT", "FILUSDT", "APTUSDT", "ARBUSDT",
]
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def _split_env(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw:
        return list(default)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _configured_symbols() -> list[str]:
    return _split_env("SYMBOLS", DEFAULT_SYMBOLS)


def _basket_symbols() -> list[str]:
    return _split_env("TOTAL_BASKET", DEFAULT_TOTAL_BASKET)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    chart_symbols = _configured_symbols()
    basket_symbols = _basket_symbols()
    all_symbols = sorted(set(chart_symbols) | set(basket_symbols))

    feeds: dict[str, SymbolFeed] = {}
    for symbol in all_symbols:
        feed = SymbolFeed(symbol)
        feeds[symbol] = feed
        await feed.start()
        log.info("started feed for %s", symbol)
        # Stagger startup so the initial REST depth snapshots don't burst
        # Binance rate limits (each /api/v3/depth?limit=5000 carries heavy
        # weight; 20 calls in quick succession triggers 429 / 418).
        await asyncio.sleep(1.5)

    basket = {sym: feeds[sym] for sym in basket_symbols if sym in feeds}
    aggregates: dict[str, AggregateFeed] = {
        "TOTAL": AggregateFeed("TOTAL", basket),
    }
    for agg in aggregates.values():
        await agg.start()
        log.info("started aggregate %s (basket=%d)", agg.name, len(agg.basket))

    app.state.feeds = feeds
    app.state.aggregates = aggregates
    app.state.chart_symbols = chart_symbols
    app.state.basket_symbols = basket_symbols
    app.state.http = httpx.AsyncClient(timeout=20.0, base_url=REST_BASE)
    try:
        yield
    finally:
        for agg in aggregates.values():
            await agg.stop()
        for feed in feeds.values():
            await feed.stop()
        await app.state.http.aclose()


app = FastAPI(title="BID/ASK Analyzer", lifespan=lifespan)


def _get_feed(symbol: str) -> SymbolFeed:
    feeds: dict[str, SymbolFeed] = app.state.feeds
    feed = feeds.get(symbol.upper())
    if feed is None:
        raise HTTPException(404, f"symbol {symbol!r} is not configured")
    return feed


def _get_data_source(name: str) -> SymbolFeed | AggregateFeed:
    """Resolve either a per-symbol feed or a cross-symbol aggregate by name."""
    key = name.upper()
    aggregates: dict[str, AggregateFeed] = app.state.aggregates
    if key in aggregates:
        return aggregates[key]
    feeds: dict[str, SymbolFeed] = app.state.feeds
    if key in feeds:
        return feeds[key]
    raise HTTPException(404, f"source {name!r} is not configured")


@app.get("/api/health")
def health() -> JSONResponse:
    feeds: dict[str, SymbolFeed] = app.state.feeds
    aggregates: dict[str, AggregateFeed] = app.state.aggregates
    return JSONResponse(
        {
            "ok": True,
            "sample_interval_seconds": SAMPLE_INTERVAL,
            "feeds": [feed.status() for feed in feeds.values()],
            "aggregates": [agg.status() for agg in aggregates.values()],
        }
    )


@app.get("/api/config")
def config() -> dict[str, Any]:
    feeds: dict[str, SymbolFeed] = app.state.feeds
    aggregates: dict[str, AggregateFeed] = app.state.aggregates
    chart_symbols: list[str] = app.state.chart_symbols
    basket_symbols: list[str] = app.state.basket_symbols
    return {
        # Symbols that have a real order book and can be charted.
        "symbols": sorted(feeds.keys()),
        # Subset that the operator explicitly wired in via env (used as the
        # default chart symbol set on the frontend).
        "chart_symbols": chart_symbols,
        # Cross-symbol aggregates available as an indicator source.
        "aggregates": sorted(aggregates.keys()),
        # ``COIN`` follows the chart symbol; aggregates are listed alongside.
        "coin_types": ["COIN", *sorted(aggregates.keys())],
        "total_basket": basket_symbols,
        "levels": list(LEVEL_KEYS),
        "diffs": list(DIFF_NAMES),
        "sample_interval_seconds": SAMPLE_INTERVAL,
    }


@app.get("/api/symbols/{symbol}/snapshot")
def snapshot(symbol: str) -> dict[str, Any]:
    src = _get_data_source(symbol)
    latest = src.history.latest
    if latest is None:
        raise HTTPException(503, "no samples yet")
    return latest


@app.get("/api/symbols/{symbol}/history")
def history(symbol: str, since: int | None = Query(default=None)) -> dict[str, Any]:
    src = _get_data_source(symbol)
    samples = src.history.since(since)
    name = src.symbol if isinstance(src, SymbolFeed) else src.name
    return {"symbol": name, "samples": samples}


@app.get("/api/symbols/{symbol}/klines")
async def klines(
    symbol: str,
    interval: str = Query(default="1h"),
    limit: int = Query(default=500, ge=1, le=1000),
    start_time: int | None = Query(default=None, alias="startTime"),
    end_time: int | None = Query(default=None, alias="endTime"),
) -> list[list[Any]]:
    # klines only make sense for a real symbol; aggregates have no price.
    feed = _get_feed(symbol)
    params: dict[str, Any] = {"symbol": feed.symbol, "interval": interval, "limit": limit}
    if start_time is not None:
        params["startTime"] = start_time
    if end_time is not None:
        params["endTime"] = end_time
    client: httpx.AsyncClient = app.state.http
    try:
        response = await client.get("/api/v3/klines", params=params)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"binance error: {exc}") from exc
    return response.json()


@app.websocket("/ws/{symbol}")
async def stream(websocket: WebSocket, symbol: str) -> None:
    try:
        src = _get_data_source(symbol)
    except HTTPException as exc:
        await websocket.close(code=4404, reason=exc.detail)
        return
    await websocket.accept()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)

    async def listener(sample: dict[str, Any]) -> None:
        try:
            queue.put_nowait(sample)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            queue.put_nowait(sample)

    src.add_listener(listener)
    try:
        latest = src.history.latest
        if latest is not None:
            await websocket.send_json({"type": "snapshot", "sample": latest})
        while True:
            sample = await queue.get()
            await websocket.send_json({"type": "sample", "sample": sample})
    except WebSocketDisconnect:
        pass
    finally:
        src.remove_listener(listener)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
