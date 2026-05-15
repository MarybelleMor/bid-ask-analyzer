// BID/ASK Analyzer — frontend.
//
// Renders a candlestick chart plus configurable sub-panes of BID/ASK/DIFF
// indicators computed by the FastAPI backend from Binance order book data.

import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
} from "lightweight-charts";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const INTERVAL_SECONDS = {
  "1m": 60,
  "5m": 5 * 60,
  "15m": 15 * 60,
  "1h": 60 * 60,
  "4h": 4 * 60 * 60,
  "1d": 24 * 60 * 60,
};

const STORAGE_KEY = "bid-ask-analyzer:v1";
const SAMPLE_CACHE_MAX = 24 * 3600; // ~24h of 1Hz samples

const PALETTE = [
  "#4caf50", "#ef5350", "#ffb74d", "#42a5f5", "#ab47bc",
  "#26a69a", "#ec407a", "#9ccc65", "#5c6bc0", "#ffa726",
  "#bdbdbd", "#66bb6a", "#e57373", "#7e57c2", "#29b6f6",
];

let palettePtr = 0;
function nextColor() {
  const c = PALETTE[palettePtr % PALETTE.length];
  palettePtr += 1;
  return c;
}

// ---------------------------------------------------------------------------
// DOM + global state
// ---------------------------------------------------------------------------

const els = {
  symbol: document.getElementById("symbol-select"),
  intervals: document.getElementById("interval-buttons"),
  chart: document.getElementById("chart"),
  paneList: document.getElementById("pane-list"),
  addPane: document.getElementById("add-pane"),
  status: document.getElementById("status"),
  priceBadge: document.getElementById("price-badge"),
  coinType: document.getElementById("coin-type-select"),
};

const state = {
  config: null,
  symbol: null,
  interval: "15m",
  // 'COIN' — indicators come from the current chart symbol's own order book.
  // 'TOTAL' (or another aggregate name) — indicators come from a cross-symbol
  // sum exposed by the backend as a virtual feed.
  coinType: "COIN",
  chart: null,
  candleSeries: null,
  volumeSeries: null,
  // Chart WS: always /ws/<chartSymbol>. Drives the live price badge and the
  // intra-bucket candle update. When coinType === 'COIN', this also drives
  // indicator series.
  chartWs: null,
  // Indicator WS: only opened when coinType is an aggregate (e.g. 'TOTAL').
  indicatorWs: null,
  klinesLast: null,
  // map seriesKey -> { series, paneIndex, buckets: Map<bucketSec, value>, indicatorKey }
  series: new Map(),
  // ordered list of user panes
  panes: [],
  activePaneId: null,
  // Samples used to redraw indicators when buckets change (e.g. TF switch).
  // Only the currently-selected indicator source feeds this cache.
  sampleCache: [],
};

function indicatorSourceName() {
  return state.coinType === "COIN" ? state.symbol : state.coinType;
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

function loadSettings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) {
    return null;
  }
}

function saveSettings() {
  const data = {
    symbol: state.symbol,
    interval: state.interval,
    coinType: state.coinType,
    panes: state.panes.map((p) => ({
      id: p.id,
      name: p.name,
      indicators: p.indicators.map((i) => ({ key: i.key, color: i.color })),
    })),
  };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch (e) { /* ignore quota errors */ }
}

// ---------------------------------------------------------------------------
// Chart construction
// ---------------------------------------------------------------------------

function buildChart() {
  if (state.chart) {
    try { state.chart.remove(); } catch (e) { /* ignore */ }
  }
  state.series.clear();

  const chart = createChart(els.chart, {
    layout: {
      background: { color: "#0e0f12" },
      textColor: "#d6d9df",
      panes: { separatorColor: "#2a2e37", separatorHoverColor: "#3a3f4a", enableResize: true },
    },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.04)" },
      horzLines: { color: "rgba(255,255,255,0.04)" },
    },
    rightPriceScale: {
      borderColor: "#2a2e37",
      scaleMargins: { top: 0.05, bottom: 0.05 },
    },
    timeScale: {
      borderColor: "#2a2e37",
      timeVisible: true,
      secondsVisible: false,
    },
    crosshair: { mode: 0 },
    autoSize: true,
  });

  const candle = chart.addSeries(CandlestickSeries, {
    upColor: "#26a69a",
    downColor: "#ef5350",
    borderUpColor: "#26a69a",
    borderDownColor: "#ef5350",
    wickUpColor: "#26a69a",
    wickDownColor: "#ef5350",
  });

  const volume = chart.addSeries(
    HistogramSeries,
    { priceFormat: { type: "volume" }, priceScaleId: "" },
    1,
  );
  volume.priceScale().applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });

  state.chart = chart;
  state.candleSeries = candle;
  state.volumeSeries = volume;

  for (let i = 0; i < state.panes.length; i += 1) {
    const pane = state.panes[i];
    pane.paneIndex = i + 2;
    for (const ind of pane.indicators) {
      addIndicatorSeries(pane.id, pane.paneIndex, ind);
    }
  }
  setTimeout(layoutPanes, 0);
}

function layoutPanes() {
  if (!state.chart) return;
  const panes = state.chart.panes();
  if (panes.length === 0) return;
  // lightweight-charts v5 distributes pane height by `stretchFactor`.
  // We want the candle pane dominant, volume small, and indicator panes
  // (each) somewhat smaller than candles.
  const factors = [5];          // candles
  if (panes.length > 1) factors.push(1); // volume
  for (let i = 2; i < panes.length; i += 1) factors.push(2);
  for (let i = 0; i < panes.length; i += 1) {
    try { panes[i].setStretchFactor(factors[i]); } catch (e) { /* ignore */ }
  }
}

window.addEventListener("resize", layoutPanes);

// ---------------------------------------------------------------------------
// Indicator series management
// ---------------------------------------------------------------------------

function seriesKey(paneId, indicatorKey) {
  return `${paneId}::${indicatorKey}`;
}

function addIndicatorSeries(paneId, paneIndex, ind) {
  const series = state.chart.addSeries(
    LineSeries,
    {
      color: ind.color,
      lineWidth: 2,
      priceFormat: { type: "volume" },
      lastValueVisible: true,
      priceLineVisible: false,
      title: ind.key,
    },
    paneIndex,
  );
  state.series.set(seriesKey(paneId, ind.key), {
    series,
    paneIndex,
    buckets: new Map(),
    indicatorKey: ind.key,
  });
}

function intervalSec() {
  return INTERVAL_SECONDS[state.interval] || 60;
}

function bucketOf(tsSec) {
  const i = intervalSec();
  return Math.floor(tsSec / i) * i;
}

function lookupIndicator(sample, key) {
  if (key.startsWith("BID ")) return sample.bid?.[key.slice(4)];
  if (key.startsWith("ASK ")) return sample.ask?.[key.slice(4)];
  if (key.startsWith("DIFF ")) return sample.diff?.[key];
  if (key === "MID") return sample.mid;
  return undefined;
}

function applySampleToSeries(sample) {
  const tsSec = Math.floor(sample.t / 1000);
  const bucket = bucketOf(tsSec);
  for (const entry of state.series.values()) {
    const value = lookupIndicator(sample, entry.indicatorKey);
    if (value === null || value === undefined || Number.isNaN(value)) continue;
    // One value per bucket: snapshot at bucket open. The indicator line
    // therefore steps at TF boundaries instead of jiggling every second.
    if (entry.buckets.has(bucket)) continue;
    entry.buckets.set(bucket, value);
    try { entry.series.update({ time: bucket, value }); } catch (e) { /* ignore */ }
  }
}

function seedSeriesFromCache() {
  if (!state.sampleCache.length) return;
  const bucketsByKey = new Map();
  for (const sample of state.sampleCache) {
    const tsSec = Math.floor(sample.t / 1000);
    const bucket = bucketOf(tsSec);
    for (const entry of state.series.values()) {
      const v = lookupIndicator(sample, entry.indicatorKey);
      if (v === null || v === undefined || Number.isNaN(v)) continue;
      let m = bucketsByKey.get(entry);
      if (!m) { m = new Map(); bucketsByKey.set(entry, m); }
      // Keep the FIRST value seen per bucket so each bucket reflects its
      // opening snapshot, matching the live-update semantics above.
      if (!m.has(bucket)) m.set(bucket, v);
    }
  }
  for (const entry of state.series.values()) {
    const m = bucketsByKey.get(entry) || new Map();
    entry.buckets = m;
    const data = [...m.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([t, value]) => ({ time: t, value }));
    try { entry.series.setData(data); } catch (e) { /* ignore */ }
  }
}

function pushSampleCache(sample) {
  state.sampleCache.push(sample);
  if (state.sampleCache.length > SAMPLE_CACHE_MAX) {
    state.sampleCache.splice(0, state.sampleCache.length - SAMPLE_CACHE_MAX);
  }
}

// ---------------------------------------------------------------------------
// Data sources
// ---------------------------------------------------------------------------

// Use `location.origin` (host without userinfo) as the API base. This matters
// when the page is served via a tunnel like https://user:pass@.../ — fetch
// throws if the request URL inherits credentials from the base URL.
const API_BASE = location.origin;

async function fetchConfig() {
  const res = await fetch(`${API_BASE}/api/config`);
  if (!res.ok) throw new Error(`config failed: ${res.status}`);
  return res.json();
}

async function fetchKlines() {
  const url = `${API_BASE}/api/symbols/${state.symbol}/klines?interval=${state.interval}&limit=500`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`klines failed: ${res.status}`);
  return res.json();
}

async function fetchHistory() {
  // Indicators always read from the configured coin-type source, not from
  // the chart symbol. With coinType=COIN they happen to coincide.
  const src = indicatorSourceName();
  const url = `${API_BASE}/api/symbols/${src}/history`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`history failed: ${res.status}`);
  return res.json();
}

function applyKlines(rows) {
  const candles = [];
  const volumes = [];
  for (const r of rows) {
    const t = Math.floor(r[0] / 1000);
    const open = parseFloat(r[1]);
    const high = parseFloat(r[2]);
    const low = parseFloat(r[3]);
    const close = parseFloat(r[4]);
    const volume = parseFloat(r[5]);
    candles.push({ time: t, open, high, low, close });
    volumes.push({
      time: t,
      value: volume,
      color: close >= open ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)",
    });
  }
  state.candleSeries.setData(candles);
  state.volumeSeries.setData(volumes);
  state.klinesLast = candles[candles.length - 1] || null;
  state.chart.timeScale().fitContent();
}

function updateKlineFromTick(price) {
  if (!state.klinesLast) return;
  const k = state.klinesLast;
  const tsSec = Math.floor(Date.now() / 1000);
  const bucket = bucketOf(tsSec);
  if (bucket === k.time) {
    k.high = Math.max(k.high, price);
    k.low = Math.min(k.low, price);
    k.close = price;
    state.candleSeries.update(k);
  } else if (bucket > k.time) {
    const fresh = { time: bucket, open: k.close, high: price, low: price, close: price };
    state.klinesLast = fresh;
    state.candleSeries.update(fresh);
  }
}

// ---------------------------------------------------------------------------
// WebSocket stream
// ---------------------------------------------------------------------------

function wsBaseUrl() {
  // location.host strips credentials, important when the page is served via
  // a basic-auth tunnel (otherwise WebSocket also rejects the URL).
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}`;
}

function closeWs(key) {
  const ws = state[key];
  state[key] = null;
  if (ws) {
    try { ws.close(); } catch (e) { /* ignore */ }
  }
}

// The chart WebSocket always tracks the chart symbol. It drives the candle
// tick and the live price badge. When coinType === 'COIN' it ALSO drives the
// indicator series, because the indicator source equals the chart symbol.
function connectChartWs() {
  closeWs("chartWs");
  const url = `${wsBaseUrl()}/ws/${state.symbol}`;
  const ws = new WebSocket(url);
  state.chartWs = ws;
  ws.onopen = () => setStatus("ok", "live");
  ws.onclose = () => {
    setStatus("bad", "disconnected");
    setTimeout(() => { if (state.symbol) connectChartWs(); }, 1500);
  };
  ws.onerror = () => setStatus("bad", "ws error");
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (!msg || !msg.sample) return;
    if (msg.sample.mid) {
      updateKlineFromTick(msg.sample.mid);
      els.priceBadge.textContent = formatPrice(msg.sample.mid);
    }
    if (state.coinType === "COIN") {
      pushSampleCache(msg.sample);
      applySampleToSeries(msg.sample);
    }
  };
}

// The indicator WebSocket only runs when coinType is an aggregate. It feeds
// the BID/ASK/DIFF series without touching the candle/price (which still come
// from the chart symbol).
function connectIndicatorWs() {
  closeWs("indicatorWs");
  if (state.coinType === "COIN") return;
  const url = `${wsBaseUrl()}/ws/${state.coinType}`;
  const ws = new WebSocket(url);
  state.indicatorWs = ws;
  ws.onclose = () => {
    if (state.coinType !== "COIN") {
      setTimeout(() => connectIndicatorWs(), 1500);
    }
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (!msg || !msg.sample) return;
    pushSampleCache(msg.sample);
    applySampleToSeries(msg.sample);
  };
}

function connectWs() {
  connectChartWs();
  connectIndicatorWs();
}

function setStatus(klass, text) {
  els.status.className = `status ${klass || ""}`;
  els.status.textContent = text;
}

// ---------------------------------------------------------------------------
// Pane / indicator UI
// ---------------------------------------------------------------------------

function renderSidebar() {
  els.paneList.innerHTML = "";
  for (const pane of state.panes) {
    els.paneList.appendChild(renderPaneCard(pane));
  }
}

function renderPaneCard(pane) {
  const card = document.createElement("div");
  card.className = "pane-card" + (pane.id === state.activePaneId ? " active" : "");
  card.dataset.paneId = pane.id;
  card.addEventListener("click", (e) => {
    if (e.target.closest("button")) return;
    state.activePaneId = pane.id;
    renderSidebar();
  });

  const header = document.createElement("div");
  header.className = "pane-card-header";
  const title = document.createElement("div");
  title.className = "pane-card-title";
  title.textContent = pane.name || `Панель ${pane.id}`;
  const actions = document.createElement("div");
  actions.className = "pane-card-actions";
  const delBtn = document.createElement("button");
  delBtn.textContent = "×";
  delBtn.title = "Удалить панель";
  delBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    removePane(pane.id);
  });
  actions.appendChild(delBtn);
  header.appendChild(title);
  header.appendChild(actions);
  card.appendChild(header);

  const tags = document.createElement("div");
  tags.className = "indicator-tags";
  for (const ind of pane.indicators) {
    const tag = document.createElement("span");
    tag.className = "indicator-tag";
    tag.style.background = "rgba(255,255,255,0.06)";
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = ind.color;
    tag.appendChild(sw);
    const txt = document.createElement("span");
    txt.textContent = ind.key;
    tag.appendChild(txt);
    const x = document.createElement("span");
    x.className = "remove";
    x.textContent = "×";
    x.addEventListener("click", (e) => {
      e.stopPropagation();
      removeIndicatorFromPane(pane.id, ind.key);
    });
    tag.appendChild(x);
    tags.appendChild(tag);
  }
  card.appendChild(tags);

  const picker = document.createElement("div");
  picker.className = "indicator-picker";
  for (const key of availableIndicators()) {
    const pill = document.createElement("button");
    pill.className =
      "indicator-pill" + (pane.indicators.some((i) => i.key === key) ? " in-use" : "");
    pill.textContent = key;
    pill.title = "ЛКМ — добавить, ПКМ — убрать";
    pill.addEventListener("click", (e) => {
      e.stopPropagation();
      addIndicatorToPane(pane.id, key);
    });
    pill.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      removeIndicatorFromPane(pane.id, key);
    });
    picker.appendChild(pill);
  }
  card.appendChild(picker);

  return card;
}

function availableIndicators() {
  const out = [];
  for (const level of state.config.levels) {
    out.push(`BID ${level}`);
    out.push(`ASK ${level}`);
  }
  for (const d of state.config.diffs) out.push(d);
  return out;
}

function addPane(name) {
  const id = "p" + Date.now().toString(36).slice(-6);
  const pane = { id, name: name || `BID/ASK SPOT COIN`, indicators: [] };
  state.panes.push(pane);
  state.activePaneId = id;
  buildChart();
  renderSidebar();
  seedSeriesFromCache();
  saveSettings();
}

function removePane(id) {
  const idx = state.panes.findIndex((p) => p.id === id);
  if (idx === -1) return;
  state.panes.splice(idx, 1);
  if (state.activePaneId === id) state.activePaneId = state.panes[0]?.id || null;
  buildChart();
  renderSidebar();
  seedSeriesFromCache();
  saveSettings();
}

function addIndicatorToPane(paneId, indicatorKey) {
  const pane = state.panes.find((p) => p.id === paneId);
  if (!pane) return;
  if (pane.indicators.some((i) => i.key === indicatorKey)) return;
  pane.indicators.push({ key: indicatorKey, color: nextColor() });
  buildChart();
  renderSidebar();
  seedSeriesFromCache();
  saveSettings();
}

function removeIndicatorFromPane(paneId, indicatorKey) {
  const pane = state.panes.find((p) => p.id === paneId);
  if (!pane) return;
  const before = pane.indicators.length;
  pane.indicators = pane.indicators.filter((i) => i.key !== indicatorKey);
  if (pane.indicators.length === before) return;
  buildChart();
  renderSidebar();
  seedSeriesFromCache();
  saveSettings();
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

function defaultPanes() {
  return [
    {
      id: "pA",
      name: "BID/ASK SPOT COIN",
      indicators: [
        { key: "DIFF 8B-3A", color: "#4caf50" },
        { key: "ASK 1.5", color: "#ef5350" },
      ],
    },
    {
      id: "pB",
      name: "BID/ASK SPOT COIN",
      indicators: [
        { key: "ASK 3", color: "#ffb74d" },
        { key: "DIFF 3B-8A", color: "#ef5350" },
      ],
    },
    {
      id: "pC",
      name: "BID/ASK SPOT COIN",
      indicators: [
        { key: "ASK 3", color: "#ff7043" },
        { key: "BID 3", color: "#4caf50" },
      ],
    },
  ];
}

function populateSymbols(symbols) {
  els.symbol.innerHTML = "";
  for (const s of symbols) {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s;
    els.symbol.appendChild(opt);
  }
}

function populateCoinTypes(types) {
  els.coinType.innerHTML = "";
  for (const t of types) {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    els.coinType.appendChild(opt);
  }
}

async function loadDataForSymbol() {
  try {
    const rows = await fetchKlines();
    applyKlines(rows);
  } catch (e) {
    console.error(e);
    setStatus("bad", "klines failed");
  }
  try {
    const hist = await fetchHistory();
    state.sampleCache = [];
    for (const sample of hist.samples) pushSampleCache(sample);
    seedSeriesFromCache();
  } catch (e) {
    console.error(e);
  }
  // Lightweight-charts may resize panes on the first setData, so re-apply our
  // layout after data is loaded.
  requestAnimationFrame(() => {
    layoutPanes();
    requestAnimationFrame(layoutPanes);
  });
}

async function boot() {
  setStatus("", "loading config…");
  state.config = await fetchConfig();
  // Prefer chart_symbols (operator-configured) for the dropdown; fall back to
  // every feed the backend exposes.
  const symbols =
    Array.isArray(state.config.chart_symbols) && state.config.chart_symbols.length
      ? state.config.chart_symbols
      : state.config.symbols;
  populateSymbols(symbols);
  const coinTypes = Array.isArray(state.config.coin_types) && state.config.coin_types.length
    ? state.config.coin_types
    : ["COIN"];
  populateCoinTypes(coinTypes);
  const saved = loadSettings();
  state.symbol =
    saved && saved.symbol && symbols.includes(saved.symbol)
      ? saved.symbol
      : symbols[0];
  if (saved && saved.interval) state.interval = saved.interval;
  state.coinType =
    saved && saved.coinType && coinTypes.includes(saved.coinType)
      ? saved.coinType
      : "COIN";
  state.panes =
    saved && Array.isArray(saved.panes) && saved.panes.length
      ? saved.panes.map((p, i) => ({
          id: p.id || "p" + i,
          name: p.name || `BID/ASK SPOT COIN`,
          indicators: (p.indicators || []).map((ind) => ({
            key: ind.key,
            color: ind.color || nextColor(),
          })),
        }))
      : defaultPanes();
  state.activePaneId = state.panes[0]?.id || null;

  els.symbol.value = state.symbol;
  els.coinType.value = state.coinType;
  for (const btn of els.intervals.querySelectorAll("button")) {
    btn.classList.toggle("active", btn.dataset.interval === state.interval);
  }

  buildChart();
  renderSidebar();
  setStatus("", "loading data…");
  await loadDataForSymbol();
  connectWs();
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

els.symbol.addEventListener("change", async () => {
  state.symbol = els.symbol.value;
  state.sampleCache = [];
  buildChart();
  renderSidebar();
  await loadDataForSymbol();
  connectWs();
  saveSettings();
});

els.coinType.addEventListener("change", async () => {
  state.coinType = els.coinType.value;
  state.sampleCache = [];
  buildChart();
  renderSidebar();
  await loadDataForSymbol();
  connectWs();
  saveSettings();
});

els.intervals.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  const interval = btn.dataset.interval;
  if (!interval) return;
  state.interval = interval;
  for (const b of els.intervals.querySelectorAll("button")) {
    b.classList.toggle("active", b === btn);
  }
  buildChart();
  renderSidebar();
  await loadDataForSymbol();
  saveSettings();
});

els.addPane.addEventListener("click", () => addPane());

function formatPrice(p) {
  if (p >= 1000) return p.toFixed(2);
  if (p >= 1) return p.toFixed(3);
  return p.toFixed(6);
}

boot().catch((e) => {
  console.error(e);
  setStatus("bad", "boot failed: " + e.message);
});
