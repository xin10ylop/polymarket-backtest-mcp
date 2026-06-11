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
| `backtest` | the strategy simulator: legacy flat params or composed bricks |
| `strategy_vocabulary` | the full schema of strategy bricks, with examples |
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

## Composable strategies (bricks)

Beyond the flat parameters, `backtest` accepts a `strategy` object composed
from a fixed vocabulary of predefined, individually tested building blocks.
Strategies are pure structured data validated against a strict schema. No
user-supplied code or expressions are ever evaluated.

Entry conditions (all must hold at the same tick; empty list means the first
tick): `price_level` (the side's ask for `<=`, bid for `>=`), `price_move`
(change in the side's mid over a trailing window, cents or percent),
`time_to_close` (seconds left), `btc_move` (BTC reference vs the market's
first tick; a signal input only, never resolution). Executions: `limit`
(maker, fills at the limit price, may never fill) or `market` (taker, fills
at the observed ask, gaps work against you). Exits, first to trigger wins:
`take_profit_cents` (maker, fills at the limit even on gaps),
`stop_loss_cents` (taker, fills at the observed bid, so a gap through the
stop costs more than the stop distance), `exit_seconds_before_close` (taker,
observed bid), and hold-to-resolution as the default. Same-tick priority is
stop, then take-profit, then time exit.

Three examples:

```json
{"side": "down",
 "entry": {"conditions": [
     {"type": "price_level", "op": "<=", "value_cents": 30},
     {"type": "time_to_close", "op": "<=", "seconds": 90}],
   "execution": {"style": "market"}},
 "exit": {}}
```
Buy Down at market when it trades under 30 cents with under 90 seconds left,
hold to resolution.

```json
{"side": "up",
 "entry": {"conditions": [
     {"type": "price_move", "window_seconds": 30, "op": ">=",
      "value": 5, "unit": "percent"}],
   "execution": {"style": "market"}},
 "exit": {"take_profit_cents": 5, "stop_loss_cents": 3}}
```
Buy Up at market on a 5 percent-in-30s upward move, 3 cent stop, 5 cent
take-profit.

```json
{"side": "up",
 "entry": {"conditions": [],
   "execution": {"style": "limit", "limit_price_cents": 40}},
 "exit": {"take_profit_cents": 5}}
```
The original v1 strategy in brick form. A regression test proves this
reproduces the audited v1 numbers exactly over the full universe.

Fees follow the venue: maker fills (limit entries, take-profits) are free;
taker fills (market entries, stops, time exits) pay the `taker_fee`
parameter, a flat fraction of notional. Real Polymarket taker fees are
odds-dependent (largest near 50 cents, roughly up to about 1.8 percent), so
the flat rate is a labeled approximation, not the venue formula.

### Walk-forward testing

Use `start_date`/`end_date` to keep parameter tuning honest: optimize on an
in-sample range, then run the chosen parameters once on the held-out range.

1. Tune: `backtest(strategy=..., start_date="2026-03-05", end_date="2026-04-08")`
2. Validate: same strategy, `start_date="2026-04-09", end_date="2026-04-25"`

The engine is no-look-ahead by construction, as verified in the audit:
ticks are processed in time order, condition evaluation at a tick reads
only that tick and earlier ones (trailing windows, forward-moving
pointers), entries strictly precede exits, and the winner is consulted
only after the last in-window tick.

Runtime: a full-universe run takes a few seconds of CPU (about 6 to 7
seconds measured locally); on a small shared host budget a couple of
minutes. For heavy multi-condition strategies start with a date range or
`max_markets`.

## Resolution rules

"Up" wins when the Chainlink BTC/USD price at window end is at or above the
price at window start. Ties resolve Up. The engine takes winners only from the
outcomes file of the 100ms dataset, never from the Binance/Coinbase/Kraken
reference prices in the data, because those prints differ from Chainlink
enough to flip close markets.

## Audit and verification

The whole pipeline went through a skeptical quant review after it was built.
The two results worth knowing:

- The headline backtest was reproduced independently. A separate script
  (`scripts/audit_independent_backtest.py`) recomputes the example strategy
  (buy Up at 40 cents, 5 cent take-profit) straight from the raw 42.7 million
  100ms ticks, bypassing the SQLite store and the engine completely. It
  produced the exact same numbers: 11,245 trades, 8,456 take-profit exits,
  4 forced wins, 2,785 forced losses, 75.23% win rate, total PnL -$688.80,
  expectancy -$0.0613 per trade. This also proves the quote-change
  deduplication in the store loses no fill information.
- Resolving on exchange prices would get 5.24% of markets wrong. 752 of the
  14,361 markets would flip their winner if it were derived from the Binance
  reference feed (last in-window print vs first) instead of taken from the
  official outcomes file. Polymarket resolves on Chainlink, and near-tie
  windows land on different sides of different feeds. That is why the engine
  takes winners only from the outcomes file and treats every BTC reference
  price as display-only.

The audit also checked store integrity (no orphan quotes, no duplicate or
out-of-order ticks, no quotes outside their market window, slug arithmetic
holds for all 15,552 markets), date-boundary behavior of the filters, output
caps on every tool, and the no-auth connector surface. The one real bug it
found (a negative `limit` could bypass the `list_markets` row cap) is fixed
and has a regression test.

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

## Deploy checklist (end to end)

1. Deploy the repo to Render (steps above) and wait for the build to go green.
2. Copy the Render URL, e.g. `https://your-app.onrender.com`.
3. Set it in `landing/index.html`: replace the `CONNECTOR_URL` constant at the
   top of the script block (one place only), keeping the `/mcp` suffix.
4. Drag the `landing/` folder onto [netlify.com](https://app.netlify.com/drop)
   to publish the static landing page.
5. Test the connector in Claude: Settings, Connectors, Add custom connector,
   paste `https://your-app.onrender.com/mcp`, then enable it in a chat via the
   plus menu and ask for a backtest.

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
