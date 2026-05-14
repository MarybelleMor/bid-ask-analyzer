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

from app.feed import SAMPLE_INTERVAL, SymbolFeed
from app.levels import DIFF_NAMES, LEVEL_KEYS
from app.orderbook import REST_BASE

log = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ["BTCUSDT"]
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def _configured_symbols() -> list[str]:
    raw = os.environ.get("SYMBOLS", "")
    if not raw:
        return DEFAULT_SYMBOLS
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    feeds: dict[str, SymbolFeed] = {}
    for symbol in _configured_symbols():
        feed = SymbolFeed(symbol)
        feeds[symbol] = feed
        await feed.start()
        log.info("started feed for %s", symbol)
    app.state.feeds = feeds
    app.state.http = httpx.AsyncClient(timeout=20.0, base_url=REST_BASE)
    try:
        yield
    finally:
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


@app.get("/api/health")
def health() -> JSONResponse:
    feeds: dict[str, SymbolFeed] = app.state.feeds
    return JSONResponse(
        {
            "ok": True,
            "sample_interval_seconds": SAMPLE_INTERVAL,
            "feeds": [feed.status() for feed in feeds.values()],
        }
    )


@app.get("/api/config")
def config() -> dict[str, Any]:
    feeds: dict[str, SymbolFeed] = app.state.feeds
    return {
        "symbols": sorted(feeds.keys()),
        "levels": list(LEVEL_KEYS),
        "diffs": list(DIFF_NAMES),
        "sample_interval_seconds": SAMPLE_INTERVAL,
    }


@app.get("/api/symbols/{symbol}/snapshot")
def snapshot(symbol: str) -> dict[str, Any]:
    feed = _get_feed(symbol)
    latest = feed.history.latest
    if latest is None:
        raise HTTPException(503, "no samples yet")
    return latest


@app.get("/api/symbols/{symbol}/history")
def history(symbol: str, since: int | None = Query(default=None)) -> dict[str, Any]:
    feed = _get_feed(symbol)
    samples = feed.history.since(since)
    return {"symbol": feed.symbol, "samples": samples}


@app.get("/api/symbols/{symbol}/klines")
async def klines(
    symbol: str,
    interval: str = Query(default="1h"),
    limit: int = Query(default=500, ge=1, le=1000),
    start_time: int | None = Query(default=None, alias="startTime"),
    end_time: int | None = Query(default=None, alias="endTime"),
) -> list[list[Any]]:
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
        feed = _get_feed(symbol)
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

    feed.add_listener(listener)
    try:
        latest = feed.history.latest
        if latest is not None:
            await websocket.send_json({"type": "snapshot", "sample": latest})
        while True:
            sample = await queue.get()
            await websocket.send_json({"type": "sample", "sample": sample})
    except WebSocketDisconnect:
        pass
    finally:
        feed.remove_listener(listener)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
