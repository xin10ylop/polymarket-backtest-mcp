# Polymarket BTC Backtester (hosted MCP server)

A public MCP server for backtesting simple limit-order strategies on
Polymarket's "BTC Up or Down" 5-minute markets. You paste one URL into
Claude as a custom connector and then ask things like:

> Backtest buying Up at 40 cents with a 5 cent take-profit across all markets
> and show me the win rate, the skew breakdown, and the date range tested.

Connector URL (replace with your deployment):

```
https://<your-app>.onrender.com/mcp
```

No login, no API key. The server is read-only and serves a static historical
dataset, so there is nothing to protect. A small per-IP rate limit keeps it
polite.

## How it works

```
 Kaggle datasets (downloaded once, at image build time)
 ┌─────────────────────────────┐  ┌──────────────────────────┐
 │ 100ms ticks + outcomes      │  │ 2s snapshots (metadata)  │
 │ namz8888, ~14k markets      │  │ debayan31415, 1,191 mkts │
 └─────────────┬───────────────┘  └────────────┬─────────────┘
               │   pmbt/ingest.py              │
               │   - in-window quotes only     │
               │   - dedup to quote changes    │
               │   - prices -> integer 1/10c   │
               │   - winners from outcomes     │
               │   - price_to_beat + x-check   │
               ▼                               ▼
         ┌───────────────────────────────────────────┐
         │ data/store.db (SQLite, read-only, ~700MB) │
         │ markets / quotes / meta                   │
         └─────────────────────┬─────────────────────┘
                               │
         ┌─────────────────────┴─────────────────────┐
         │ pmbt/server.py (FastMCP, streamable HTTP) │
         │  /mcp     5 tools                         │
         │  /        landing page                    │
         │  /health  liveness                        │
         └─────────────────────┬─────────────────────┘
                               │ HTTPS (Render)
                               ▼
                  Claude custom connector
```

The backtest engine (`pmbt/engine.py`) is pure functions over integer prices.
It knows nothing about BTC reference prices or price_to_beat, so display data
can never leak into PnL.

## Tools

| tool | what it does |
|---|---|
| `data_coverage` | date range, market counts, list of missing or partial days |
| `list_markets` | browse markets with winner, price_to_beat, tradeability |
| `get_price_series` | bid/ask series for both sides plus BTC reference, resampled |
| `backtest` | the strategy simulator, see below |
| `market_context` | one market in full: window, strike, winner, tick coverage |

## The strategy that gets backtested

For each market in the chosen date range:

1. Rest a limit buy on the chosen side at your entry price. It fills at the
   first tick where that side's best ask is at or below the entry price,
   always at the entry price exactly. One trade per market, full size.
2. On entry, rest a limit sell at entry plus the take-profit. It fills at the
   first later tick where the best bid reaches the target, always at the limit
   price, even when the bid gaps through it.
3. If the take-profit never fills, the position rides to resolution: $1.00 if
   the held side won, $0.00 if it lost.

Stated assumptions (also returned by every backtest call):

- Touch-fill: resting orders fill when the opposing best quote reaches their
  level. No queue position or book depth model. Empirically reasonable in
  these very liquid markets, still an approximation.
- Both legs are maker orders and Polymarket makers pay zero fees here, so the
  default fee is 0. A `taker_fee` override exists for simulating market orders.
- Redemption at $1/$0 is free.
- Static dataset. Past quotes, not live ones.

The results make the skew of this strategy very visible: win rates above 70%
with negative expectancy, because the upside is capped at the take-profit
while a forced loss costs the whole entry price.

## Resolution rules

"Up" wins when the Chainlink BTC/USD price at window end is at or above the
price at window start. Ties resolve Up. The engine takes winners only from the
outcomes file of the 100ms dataset, never from the Binance/Coinbase/Kraken
reference prices in the data, because those prints differ from Chainlink
enough to flip close markets.

## Data

Two Kaggle datasets, credit where due:

- [Polymarket BTC 5-Minute 100ms Market Data](https://www.kaggle.com/datasets/namz8888/polymarket-btc-5-minute-high-frequency-tick-data)
  by namz8888. The backbone: 100ms quotes and resolved outcomes for ~14,000
  markets over about 7 weeks (2026-03-05 to 2026-04-25). The tradeable
  universe is exactly the markets here that have both quotes and an outcome.
- [Polymarket 5 minutes BTC UP Down data](https://www.kaggle.com/datasets/debayan31415/polymarket-5-minutes-btc-up-down-data)
  by debayan31415. 2s snapshots for 1,191 markets (2026-02-23 to 2026-03-05).
  Used only for the official price_to_beat and for cross-checking winners.
  Markets that exist only here are never backtested, since 2s sampling misses
  price touches.

The two datasets currently share zero markets (they cover adjacent date
ranges), so cross-validation had nothing to flag and tradeable markets show an
approximate price_to_beat taken from the first reference tick. That value is
display only. See `docs/DATA_SCHEMA.md` for the full schema and decisions.

## Run it locally

```bash
pip install -r requirements.txt

# build the data store (~600MB download, a few minutes)
python -m pmbt.ingest --download --raw data_raw --db data/store.db

# tests
python -m pytest tests/ -q

# serve
PORT=8000 python -m pmbt.server
# landing page: http://localhost:8000/
# MCP endpoint: http://localhost:8000/mcp
```

## Deploy to Render

The Dockerfile downloads the datasets and builds the SQLite store during the
image build, so the running container never fetches data. Host disks being
ephemeral does not matter; the store ships inside the image.

1. Push this repo to GitHub.
2. On [render.com](https://render.com): New, then Web Service, then connect
   the repo. Render picks up `render.yaml` and the Dockerfile automatically
   (runtime: Docker, health check on `/health`). The starter plan is enough;
   the image is about 1GB because of the baked store.
3. Wait for the build (the Kaggle download runs inside it, expect ~10 min).
4. Your connector URL is `https://<app-name>.onrender.com/mcp`. HTTPS is
   automatic. Open the root URL to see the landing page with the copy button.

Railway and Fly work the same way: deploy the Dockerfile, expose `$PORT`,
done. The server listens on `0.0.0.0:$PORT` and defaults to 8000.

## Add it to Claude

1. Settings, then Connectors, then Add custom connector.
2. Paste the `/mcp` URL. No auth fields needed; the connector flow probes the
   OAuth discovery endpoints, gets clean 404s, and falls back to anonymous.
3. In a chat, open the plus menu and enable the connector.
4. Ask for a backtest.

## Repo layout

```
pmbt/
  ingest.py    Kaggle files -> SQLite store
  store.py     store schema + connections
  db.py        read-only query layer
  engine.py    backtest engine (pure, integer prices)
  server.py    FastMCP server, tools, rate limiting
  landing.py   the landing page HTML
scripts/
  discover_schema.py   raw-schema dump (run before trusting any column name)
docs/
  DATA_SCHEMA.md             schemas and ingest decisions
  schema_discovery_output.txt captured discovery output
tests/         engine + store tests (22)
Dockerfile     two-stage build, store baked at build time
render.yaml    Render blueprint
```

Not financial advice. Built as a portfolio project.
