# BID/ASK Analyzer — бесплатный аналог индикаторов trading-platform.ru

Веб-приложение, которое собирает стакан лимитных ордеров с Binance в реальном
времени и показывает агрегированные объёмы на разных дистанциях от цены — то,
что в [trading-platform.ru](https://trading-platform.ru/) называется
индикатором **BID/ASK SPOT COIN**.

Подключение к Binance идёт через публичные read-only эндпоинты
`data-api.binance.vision` / `data-stream.binance.vision`, поэтому никакие API
ключи и аутентификация не нужны.

## Что считается

Для каждого уровня **P ∈ {1.5%, 3%, 5%, 8%, 15%, 30%, 60%}** на каждой итерации
агрегации (~1 раз в секунду):

```
mid_price = (best_bid + best_ask) / 2
BID P = Σ price·quantity по бидам с price ≥ mid·(1 - P/100)
ASK P = Σ price·quantity по аскам с price ≤ mid·(1 + P/100)
```

То есть это **накопленный объём лимитных ордеров в USD** в заданном ценовом
коридоре от текущей цены. Чем дальше уровень, тем больше включаемых ордеров.

### DIFF-линии

Симметричные:

```
DIFF P = BID P − ASK P            (для каждого P)
```

Кросс-уровневые (BID на одном уровне минус ASK на другом):

```
DIFF 3B-8A   = BID 3  − ASK 8
DIFF 8B-3A   = BID 8  − ASK 3
DIFF 8B-30A  = BID 8  − ASK 30
DIFF 5B-15A  = BID 5  − ASK 15
DIFF 15B-5A  = BID 15 − ASK 5
DIFF 8B-15A  = BID 8  − ASK 15
DIFF 15B-30A = BID 15 − ASK 30
DIFF 30B-15A = BID 30 − ASK 15
```

«Кольцевые» (объём в кольце между двумя уровнями, BID + ASK на обеих сторонах):

```
DIFF outer-inner = (BID outer − BID inner) + (ASK outer − ASK inner)
```

(для пар 30-15, 30-8, 15-8, 8-5).

## Архитектура

```
[Binance data-stream] --WS--> [SymbolFeed] -- 1Hz --> [HistoryBuffer + listeners]
                                                            |
                                  ┌───── REST /api/* ────────┘
                                  │
                                  └───── WS /ws/{symbol} ─→ браузер
```

* `backend/` — FastAPI приложение, которое:
  * подписывается на `wss://data-stream.binance.vision/ws/<symbol>@depth@100ms`;
  * подтягивает REST-снимок `data-api.binance.vision/api/v3/depth?limit=5000`
    и сшивает с диффами по протоколу из доков Binance;
  * раз в секунду пересчитывает все BID/ASK/DIFF уровни и хранит последние
    24 часа в кольцевом буфере;
  * раздаёт данные через REST (`/api/symbols/{sym}/snapshot|history|klines`)
    и WebSocket (`/ws/{sym}`).

* `frontend/` — статический HTML + ES modules, использующий
  [`lightweight-charts`](https://github.com/tradingview/lightweight-charts)
  v5 (мульти-пейновая поддержка). Свечной график BTC/USDT + несколько
  сабпейнов с настраиваемыми BID/ASK/DIFF линиями.

## Локальный запуск

Требуется Python ≥ 3.11.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/uvicorn app.main:app --port 8765
```

Откройте http://localhost:8765 — фронтенд раздаётся из того же процесса.

### Конфигурация

Через переменные окружения:

* `SYMBOLS` — список тикеров через запятую (по умолчанию `BTCUSDT`).
  Например: `SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT`.
* `LOG_LEVEL` — уровень логирования (по умолчанию `INFO`).

## API

* `GET /api/health` — состояние всех фидов (готовность, размер стакана).
* `GET /api/config` — список доступных уровней и DIFF-комбинаций.
* `GET /api/symbols/{sym}/snapshot` — последняя агрегированная выборка.
* `GET /api/symbols/{sym}/history?since={ms}` — все выборки из памяти,
  опционально новее `since` (UNIX ms).
* `GET /api/symbols/{sym}/klines?interval=15m&limit=500` — свечи Binance,
  прокси к `data-api.binance.vision`.
* `WS  /ws/{sym}` — поток выборок (~1Hz).

Формат выборки (JSON):

```json
{
  "t": 1700000000000,
  "mid": 81725.5,
  "best_bid": 81725.4,
  "best_ask": 81725.6,
  "bid": {"1.5": 17341412.83, "3": 18992703.24, "5": 19011829.27},
  "ask": {"1.5": 24515101.22, "3": 24515852.97, "5": 24515852.97},
  "diff": {"DIFF 1.5": -7173688.39, "DIFF 3B-8A": -5527338.43}
}
```

## Ограничения и что можно улучшить

* **История только с момента запуска сервиса.** Сейчас выборки хранятся в
  памяти (24 часа кольцевого буфера). После перезапуска бэкенд теряет всю
  историю. Для полноценной истории нужно писать в SQLite/Postgres.
* **Только спот-данные.** WebSocket фьючерсов (`fstream.binance.com`)
  блокируется из некоторых регионов; нужна отдельная реализация под
  `fapi.binance.com` или использование зеркала.
* **5000 уровней стакана.** Binance отдаёт максимум 5000 уровней с каждой
  стороны через REST. Для дальних уровней (30%/60% от цены) на тонких рынках
  этого может не хватать. Можно дополнительно подписаться на trade-стримы и
  оценивать ликвидность по сделкам.
* **Resampling на фронте.** Сейчас 1Hz выборки бакетятся фронтендом под
  интервал свечи (`last value wins`). Это упрощение — оригинальный
  индикатор, вероятно, делает OHLC по объёму.

## Тесты

```bash
cd backend
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## Лицензия

MIT.
