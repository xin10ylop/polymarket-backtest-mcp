"""Public MCP server: Polymarket BTC Up/Down backtesting.

Streamable HTTP transport at /mcp, landing page at /, health at /health.
No auth (read-only public data); light per-IP rate limiting instead.
"""

import os
import time
from collections import deque

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import engine
from .db import Store, iso, parse_date
from .landing import landing_html

mcp = FastMCP(
    name="Polymarket BTC Up/Down Backtester",
    instructions=(
        "Backtest resting-limit strategies on Polymarket's 'BTC Up or Down' "
        "5-minute markets using historical 100ms quote data. Start with "
        "data_coverage() to see the tested period. Prices are dollars "
        "(0.40 = 40 cents); take-profit is in cents."
    ),
)

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


@mcp.tool
def data_coverage() -> dict:
    """Date range, market counts and per-day gaps of the historical dataset.

    Call this first: every backtest result is conditional on this coverage
    window. Days listed in date_gaps have missing or partial data.
    """
    return store().coverage()


@mcp.tool
def list_markets(
    duration_minutes: int = 5,
    resolved: bool | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List markets with window times, winner, price_to_beat and tradeability.

    start_date/end_date are 'YYYY-MM-DD' (UTC, end inclusive). limit is capped
    at 1000. Markets with tradeable=false exist only in the low-resolution 2s
    dataset and are excluded from backtests.
    """
    rows = store().list_markets(
        duration_minutes=duration_minutes,
        resolved=resolved,
        start_ms=parse_date(start_date),
        end_ms=parse_date(end_date, end=True),
        limit=limit,
        offset=offset,
    )
    return {"count": len(rows), "offset": offset, "markets": rows}


@mcp.tool
def get_price_series(
    market_id: str,
    resample: str = "1s",
    max_rows: int = 600,
) -> dict:
    """Bid/ask series for both sides of one market plus the BTC reference feed.

    market_id is the market slug (e.g. 'btc-updown-5m-1772733900').
    resample: '1s', '5s', '10s', '30s', '60s' or 'raw' (raw 100ms quote
    changes, only on explicit request). Output is always capped at max_rows
    (hard cap 3000); BTC reference prices are display only and never used
    for resolution.
    """
    st = store()
    m = st.get_market(market_id)
    if not m:
        return {"error": f"unknown market: {market_id}"}
    if not m["tradeable"]:
        return {
            "error": f"{market_id} has no 100ms quote data (2s-only market); "
            "price series unavailable",
            "tradeable": False,
        }
    steps = {"1s": 1000, "5s": 5000, "10s": 10000, "30s": 30000, "60s": 60000}
    if resample != "raw" and resample not in steps:
        return {"error": f"resample must be one of {list(steps)} or 'raw'"}
    max_rows = max(1, min(int(max_rows), 3000))

    rows = st.quotes(market_id)
    out = []
    if resample == "raw":
        sel = rows
    else:
        step = steps[resample]
        sel, next_t = [], None
        # last-known quote state at or before each bucket edge
        for r in rows:
            if next_t is None:
                next_t = (r["ts_ms"] // step) * step + step
                sel.append(r)
                continue
            if r["ts_ms"] >= next_t:
                sel.append(r)
                next_t = (r["ts_ms"] // step) * step + step
    for r in sel[:max_rows]:
        out.append({
            "ts_ms": r["ts_ms"],
            "up_bid": r["up_bid"] / 1000 if r["up_bid"] >= 0 else None,
            "up_ask": r["up_ask"] / 1000 if r["up_ask"] >= 0 else None,
            "down_bid": r["down_bid"] / 1000 if r["down_bid"] >= 0 else None,
            "down_ask": r["down_ask"] / 1000 if r["down_ask"] >= 0 else None,
            "btc_ref": r["btc_ref"],
        })
    return {
        "market": market_id,
        "window_start_utc": iso(m["window_start_ms"]),
        "window_end_utc": iso(m["window_end_ms"]),
        "price_to_beat": m["price_to_beat"],
        "price_to_beat_source": m["price_to_beat_source"],
        "resample": resample,
        "rows_returned": len(out),
        "truncated": len(sel) > max_rows,
        "series": out,
        "note": "btc_ref is an exchange reference feed (display only); "
                "Polymarket resolves on Chainlink BTC/USD.",
    }


@mcp.tool
def backtest(
    side: str = "up",
    entry_price: float = 0.40,
    take_profit_cents: float = 5.0,
    taker_fee: float = 0.0,
    duration_minutes: int = 5,
    start_date: str | None = None,
    end_date: str | None = None,
    max_markets: int | None = None,
) -> dict:
    """Backtest a resting-limit entry with a fixed take-profit, one trade per market.

    Strategy: rest a limit BUY on the chosen side at entry_price (dollars,
    e.g. 0.40). It fills at the first tick where that side's best ask <= entry
    price. On fill, rest a limit SELL at entry_price + take_profit_cents; it
    fills at the limit price at the first later tick where the best bid >= the
    target. If the take-profit never fills, the position force-resolves at
    window end: $1.00 if the held side won, $0.00 otherwise. Maker fees are 0
    on Polymarket, so taker_fee defaults to 0 (override to simulate market
    orders). Dates are 'YYYY-MM-DD' UTC, end inclusive.
    """
    try:
        st = store()
        start_ms = parse_date(start_date)
        end_ms = parse_date(end_date, end=True)
        markets = st.markets_for_backtest(
            duration_minutes=duration_minutes,
            start_ms=start_ms,
            end_ms=end_ms,
            max_markets=max_markets,
        )
        res = engine.run_backtest(
            markets, lambda s: st.side_quotes(s, side), side,
            entry_price, take_profit_cents, taker_fee=taker_fee,
        )
    except ValueError as e:
        return {"error": str(e)}

    summary = engine.summarize(res)
    starts = [m["window_start_ms"] for m in markets]
    days = {iso(t)[:10] for t in starts}
    coverage = {
        "start_utc": iso(min(starts)) if starts else None,
        "end_utc": iso(max(starts)) if starts else None,
        "days_covered": len(days),
        "markets_in_range": res.markets_in_range,
        "markets_tested": res.markets_tested,
        "untriggered_markets": res.untriggered_markets,
    }
    entry_c = round(entry_price * 100, 1)
    return {
        "params": {
            "side": side, "entry_price": entry_price,
            "take_profit_cents": take_profit_cents, "taker_fee": taker_fee,
            "duration_minutes": duration_minutes,
        },
        "coverage": coverage,
        **summary,
        "assumptions": engine.ASSUMPTIONS,
        "skew_note": (
            f"Negative skew: upside is capped at {take_profit_cents}c per trade, "
            f"but a forced loss costs the full {entry_c}c entry price."
        ),
    }


@mcp.tool
def market_context(market_id: str) -> dict:
    """Full context for one market: window, price_to_beat, winner, tick coverage.

    price_to_beat_source 'official' comes from the supplementary 2s dataset;
    'approx_from_reference' is the exchange reference price at the first
    in-window tick (display only, not the Chainlink strike). Resolution always
    comes from the outcomes file, never from reference prices.
    """
    st = store()
    m = st.get_market(market_id)
    if not m:
        return {"error": f"unknown market: {market_id}"}
    dur_ms = m["window_end_ms"] - m["window_start_ms"]
    expected_ticks = dur_ms // 100
    return {
        "slug": m["slug"],
        "window_start_utc": iso(m["window_start_ms"]),
        "window_end_utc": iso(m["window_end_ms"]),
        "window_start_ms": m["window_start_ms"],
        "window_end_ms": m["window_end_ms"],
        "duration_minutes": m["duration_minutes"],
        "winner": m["winner"],
        "resolution_source": m["resolution_source"],
        "price_to_beat": m["price_to_beat"],
        "price_to_beat_source": m["price_to_beat_source"],
        "btc_ref_first": m["btc_ref_first"],
        "btc_ref_last": m["btc_ref_last"],
        "tick_coverage": {
            "in_window_ticks": m["tick_count"],
            "expected_100ms_ticks": expected_ticks,
            "coverage_pct": round(100 * m["tick_count"] / expected_ticks, 1)
            if expected_ticks else None,
        },
        "tradeable": bool(m["tradeable"]),
    }


# ----------------------------------------------------------------- routes

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return PlainTextResponse("ok")


@mcp.custom_route("/", methods=["GET"])
async def landing(request: Request):
    try:
        cov = store().coverage()
    except Exception:
        cov = {}
    base = str(request.base_url).rstrip("/")
    return HTMLResponse(landing_html(connector_url=f"{base}/mcp", coverage=cov))


# Claude's connector flow probes OAuth discovery endpoints; a clean 404 makes
# the client fall back to anonymous connection.
for _p in (
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/openid-configuration",
    "/register",
):
    @mcp.custom_route(_p, methods=["GET", "POST"])
    async def _no_oauth(request: Request):
        return JSONResponse({"error": "not_found"}, status_code=404)


class RateLimiter:
    """Per-IP sliding window over the ASGI app. No auth by design."""

    def __init__(self, app, max_requests=120, window_s=60):
        self.app = app
        self.max = max_requests
        self.window = window_s
        self.hits: dict[str, deque] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            ip = None
            for k, v in scope.get("headers", []):
                if k == b"x-forwarded-for":
                    ip = v.decode().split(",")[0].strip()
                    break
            if not ip and scope.get("client"):
                ip = scope["client"][0]
            now = time.monotonic()
            dq = self.hits.setdefault(ip or "?", deque())
            while dq and dq[0] < now - self.window:
                dq.popleft()
            if len(dq) >= self.max:
                resp = JSONResponse({"error": "rate_limited"}, status_code=429)
                await resp(scope, receive, send)
                return
            dq.append(now)
            if len(self.hits) > 10_000:  # bound memory
                self.hits = {k: v for k, v in self.hits.items() if v}
        await self.app(scope, receive, send)


def build_app():
    app = mcp.http_app(path="/mcp", stateless_http=True)
    return RateLimiter(app)


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
