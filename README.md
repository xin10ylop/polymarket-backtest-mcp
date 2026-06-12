# Polymarket BTC Backtester

An MCP server you add to Claude with one URL. Once connected, you can ask
Claude things like "backtest buying Up at 40 cents with a 5 cent take-profit"
and it will run that strategy against 14,361 real Polymarket "BTC Up or Down"
5-minute markets and tell you the win rate, the profit per trade, and where
the strategy breaks.

That's the whole idea. Claude is the interface, this server does the math.
No signup, no API key. You paste the URL, enable the connector in a chat,
and ask away.

```
https://<your-app>.onrender.com/mcp     <- your URL after deploying (see below)
```

## Why this exists

Polymarket runs a market every 5 minutes on whether Bitcoin will end the
window higher or lower. The token prices move fast and the strategies people
come up with sound great on paper. Most of them lose money, and the way they
lose is sneaky: a strategy can win 75% of the time and still bleed out,
because wins are capped at a few cents while losses eat the whole entry
price. Run it here first, on real historical data, before any of it touches
a wallet.

The numbers behind that example, from this very dataset: buying Up at 40
cents with a 5 cent take-profit wins 75.2% of the time and still loses 6
cents per trade. Break-even would need 88.9%.

## What you can ask

Plain English works. Claude reads the tool schema and turns your request
into a strategy spec.

- "Backtest buying Up at 40 cents with a 5 cent take-profit across all markets"
- "Buy Down at market when it's under 30 cents with less than 90 seconds left, hold to resolution"
- "Buy Up when it jumps 5% in 30 seconds, with a 3 cent stop and a 5 cent take-profit"
- "Optimize a strategy on March data, then validate it out-of-sample on April"

Strategies are built from fixed pieces the server knows how to run: entry
conditions (price level, price momentum, time left, BTC move), limit or
market entries, and take-profit, stop-loss or timed exits. Claude never
sends code, only structured parameters checked against a schema. The fixed
vocabulary is what makes the engine trustworthy: every piece is unit tested
on its own.

## The tools

| tool | what it does |
|---|---|
| `backtest` | runs a strategy over the historical markets, one trade per market |
| `strategy_vocabulary` | the full schema of strategy pieces, with examples |
| `data_coverage` | the exact date range behind every result, plus gap days |
| `list_markets` | browse markets with winners and metadata |
| `get_price_series` | bid/ask history for one market, resampled |
| `market_context` | everything known about a single market |

Every backtest answer includes the date range actually tested, the exit
breakdown (take-profits vs stops vs forced resolutions), the fill
assumptions, and an equity curve.

## How the simulation works

The engine tries to be honest about how orders actually fill:

- A resting limit order fills at your price when the opposing quote touches
  it. Makers pay no fees on these markets, so those fills are free.
- A market order fills at whatever the quote actually is at that moment, so
  gaps hurt you, same as live. Taker fills can carry a flat fee you set.
- A stop-loss is a market order: if the bid gaps from 38 to 20 cents, you
  get 20, not your 35 cent stop. The take-profit is the opposite: it is a
  resting order, so it fills at your limit even when the price jumps past it.
- If nothing exits before the window closes, the position resolves at $1.00
  or $0.00 based on the official market outcome. Ties go to Up, per
  Polymarket's rules.
- All prices are integers internally (tenths of a cent), so there is no
  float rounding anywhere in the fill logic.
- One trade per market, ticks processed in time order, and nothing can peek
  ahead: a condition at a given tick only sees that tick and earlier ones,
  and the winner is only consulted after the window ends.

## The data

Two Kaggle datasets, full credit to their authors:

- [Polymarket BTC 5-Minute 100ms Market Data](https://www.kaggle.com/datasets/namz8888/polymarket-btc-5-minute-high-frequency-tick-data)
  by namz8888. The backbone: 100ms bid/ask snapshots and official outcomes
  for 14,361 markets across 52 days (March 5 to April 25, 2026). After
  deduplication that's 6.9 million quote changes.
- [Polymarket 5 minutes BTC UP Down data](https://www.kaggle.com/datasets/debayan31415/polymarket-5-minutes-btc-up-down-data)
  by debayan31415. A 2-second dataset used only for metadata, like the
  official strike price. Its markets are never traded on, since 2-second
  sampling misses the price touches the fill logic depends on.

The two datasets cover adjacent date ranges and share zero markets, so the
strike prices shown for tradeable markets come from an exchange reference
feed and are marked as approximate. They are display only.

One thing worth stressing: winners always come from the official outcomes
file, never from BTC exchange prices. Polymarket resolves on Chainlink, and
the feeds disagree enough that 752 of the 14,361 markets (5.24%) would flip
their result if you resolved them on the Binance feed instead. The engine
is built so reference prices physically cannot reach the PnL code.

## Audit

After the build, the whole thing went through a skeptical review. The
headline backtest was recomputed by a separate script straight from the raw
42.7 million ticks, skipping the database and the engine entirely, and it
landed on the same numbers to the cent: 11,245 trades, total PnL of
-$688.80, expectancy of -$0.0613. That check now lives in the test suite as
a permanent regression anchor, next to 47 other tests covering fills,
resolution ties, look-ahead safety, fee handling, and schema validation.

## Architecture

```
 Kaggle datasets (downloaded once, at image build time)
 ┌─────────────────────────────┐  ┌──────────────────────────┐
 │ 100ms ticks + outcomes      │  │ 2s snapshots (metadata)  │
 └─────────────┬───────────────┘  └────────────┬─────────────┘
               │   pmbt/ingest.py              │
               ▼                               ▼
         ┌───────────────────────────────────────────┐
         │ data/store.db (SQLite, read-only, ~700MB) │
         └─────────────────────┬─────────────────────┘
                               │
         ┌─────────────────────┴─────────────────────┐
         │ pmbt/server.py (FastMCP, streamable HTTP) │
         │  /mcp     six tools                       │
         │  /        landing page                    │
         │  /health  liveness                        │
         └─────────────────────┬─────────────────────┘
                               │ HTTPS (Render)
                               ▼
                  Claude custom connector
```

## Run it locally

```bash
pip install -r requirements.txt

# build the data store (downloads ~600MB from Kaggle, takes a few minutes)
python -m pmbt.ingest --download --raw data_raw --db data/store.db

# tests (includes the full-data regression anchor when the store exists)
python -m pytest tests/ -q

# serve
PORT=8000 python -m pmbt.server
# landing page: http://localhost:8000/
# MCP endpoint: http://localhost:8000/mcp
```

## Deploy

The Dockerfile downloads the datasets and builds the SQLite store during
the image build, so the running container never needs the network for data.

1. Push this repo to GitHub.
2. On [render.com](https://render.com): New, then Web Service, then connect
   the repo. Render picks up `render.yaml` and the Dockerfile on its own.
   The build takes around 10 minutes because the Kaggle download runs
   inside it.
3. Your connector URL is `https://<app-name>.onrender.com/mcp`.
4. Open `landing/index.html`, put that URL into the `CONNECTOR_URL`
   constant at the top of the script block (it lives in that one spot), and
   drag the `landing/` folder onto [netlify.com](https://app.netlify.com/drop)
   to publish the standalone landing page.
5. Test it in Claude: Settings, Connectors, Add custom connector, paste the
   URL, then enable it in a chat through the + menu.

Railway and Fly work too: deploy the Dockerfile, expose `$PORT`, done.

## Notes on auth and limits

There is no auth on purpose. The server is read-only over a static dataset,
and Claude's custom connector flow only supports authless or OAuth servers.
A light per-IP rate limit keeps things polite, and slow full-universe runs
include a hint suggesting a date range.

## Repo layout

```
pmbt/
  ingest.py      Kaggle files -> SQLite store
  store.py       store schema and connections
  db.py          read-only query layer
  engine.py      the original v1 engine, kept as the audited reference
  strategy.py    the composable strategy engine
  server.py      FastMCP server, tools, rate limiting
  landing.py     serves the landing page template
scripts/
  discover_schema.py            schema dump of the raw files
  audit_independent_backtest.py the independent recompute from the audit
docs/            data schema and decisions
tests/           48 tests
landing/         standalone page for Netlify (drag and drop)
Dockerfile       two-stage build, store baked in at build time
render.yaml      Render blueprint
```

Educational and research use on static historical data. Not financial
advice. Not affiliated with Polymarket.
