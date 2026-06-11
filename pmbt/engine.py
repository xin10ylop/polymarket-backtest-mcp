"""Backtest engine for the resting-limit entry / take-profit strategy.

All prices inside the engine are integer tenths of a cent (0.40 -> 400),
so fill checks are exact integer comparisons with no float drift.

The engine knows nothing about price_to_beat or BTC reference prices.
PnL depends only on token quotes and the market's resolved winner.
"""

from dataclasses import dataclass, field

MILLI = 1000  # tenths of a cent per dollar

ASSUMPTIONS = [
    "Resting limit orders fill at their limit price the first time the opposing "
    "best quote touches their level (touch-fill, full size, no queue or depth model).",
    "Both legs are maker orders; Polymarket makers pay zero fees on these markets, "
    "so the default fee is 0. taker_fee is an optional override.",
    "The take-profit is evaluated only on ticks strictly after the entry tick: "
    "an entry and its exit can never fill on the same tick.",
    "If the take-profit never fills, the position is force-resolved at window end: "
    "$1.00 if the held side won, $0.00 if it lost. No fee on redemption.",
    "Static historical dataset; one trade per market maximum.",
]


def dollars_to_milli(price: float) -> int:
    return int(round(price * MILLI))


def cents_to_milli(cents: float) -> int:
    return int(round(cents * 10))


@dataclass
class Trade:
    slug: str
    window_start_ms: int
    entry_ts_ms: int
    entry_milli: int
    exit_kind: str  # 'take_profit' | 'forced_win' | 'forced_loss'
    exit_milli: int
    pnl_milli: int


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    markets_in_range: int = 0
    markets_tested: int = 0
    untriggered_markets: int = 0


def simulate_market(quotes, winner, side, entry_milli, tp_milli, taker_fee=0.0):
    """Simulate one market. Returns a Trade or None (untriggered).

    quotes: iterable of (ts_ms, bid_milli, ask_milli) for the CHOSEN side only,
            in time order, in-window only. -1 marks a missing quote and never
            triggers a fill.
    winner: 'up' or 'down' (resolution ground truth, from the outcomes file).
    """
    entry_i = None
    entry_ts = None
    for i, (ts_ms, bid, ask) in enumerate(quotes):
        if 0 <= ask <= entry_milli:
            entry_i, entry_ts = i, ts_ms
            break
    if entry_i is None:
        return None

    exit_kind, exit_milli = None, None
    for ts_ms, bid, ask in quotes[entry_i + 1:]:
        # maker sell fills at the LIMIT price even when the bid gaps through it
        if bid >= tp_milli:
            exit_kind, exit_milli = "take_profit", tp_milli
            break
    if exit_kind is None:
        if winner == side:
            exit_kind, exit_milli = "forced_win", MILLI
        else:
            exit_kind, exit_milli = "forced_loss", 0

    pnl = exit_milli - entry_milli
    if taker_fee:
        # optional override for users simulating taker (market) orders:
        # fee charged on both traded legs as a fraction of notional, never on redemption
        fee = taker_fee * entry_milli
        if exit_kind == "take_profit":
            fee += taker_fee * exit_milli
        pnl -= int(round(fee))
    return Trade(
        slug="", window_start_ms=0, entry_ts_ms=entry_ts,
        entry_milli=entry_milli, exit_kind=exit_kind,
        exit_milli=exit_milli, pnl_milli=pnl,
    )


def run_backtest(markets, quotes_for, side, entry_price, take_profit_cents,
                 taker_fee=0.0):
    """markets: list of dicts with slug, window_start_ms, winner, tradeable.
    quotes_for: callable slug -> list of (ts_ms, bid_milli, ask_milli) tuples
                for the chosen side, in-window, time ordered.
    """
    if side not in ("up", "down"):
        raise ValueError("side must be 'up' or 'down'")
    if not (0 < entry_price < 1):
        raise ValueError("entry_price must be between 0 and 1 (dollars)")
    if take_profit_cents <= 0:
        raise ValueError("take_profit_cents must be positive")
    entry_milli = dollars_to_milli(entry_price)
    tp_milli = entry_milli + cents_to_milli(take_profit_cents)

    res = BacktestResult(markets_in_range=len(markets))
    for m in markets:
        if not m["tradeable"]:
            continue
        res.markets_tested += 1
        trade = simulate_market(
            quotes_for(m["slug"]), m["winner"], side, entry_milli, tp_milli,
            taker_fee=taker_fee,
        )
        if trade is None:
            res.untriggered_markets += 1
            continue
        trade.slug = m["slug"]
        trade.window_start_ms = m["window_start_ms"]
        res.trades.append(trade)
    res.trades.sort(key=lambda t: t.window_start_ms)
    return res


def summarize(res: BacktestResult, max_equity_points: int = 200):
    trades = res.trades
    n = len(trades)
    wins = [t for t in trades if t.pnl_milli > 0]
    losses = [t for t in trades if t.pnl_milli <= 0]
    total_pnl = sum(t.pnl_milli for t in trades)

    equity, peak, max_dd = [], 0, 0
    cum = 0
    for t in trades:
        cum += t.pnl_milli
        equity.append((t.window_start_ms, cum))
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    if len(equity) > max_equity_points:
        last = len(equity) - 1
        idx = sorted({round(i * last / (max_equity_points - 1))
                      for i in range(max_equity_points)})
        equity = [equity[i] for i in idx]

    breakdown = {
        "take_profit_exits": sum(1 for t in trades if t.exit_kind == "take_profit"),
        "forced_resolution_wins": sum(1 for t in trades if t.exit_kind == "forced_win"),
        "forced_resolution_losses": sum(1 for t in trades if t.exit_kind == "forced_loss"),
    }
    return {
        "stats": {
            "num_trades": n,
            "win_rate": round(len(wins) / n, 4) if n else None,
            "avg_win": round(sum(t.pnl_milli for t in wins) / len(wins) / MILLI, 4) if wins else None,
            "avg_loss": round(sum(t.pnl_milli for t in losses) / len(losses) / MILLI, 4) if losses else None,
            "expectancy": round(total_pnl / n / MILLI, 4) if n else None,
            "total_pnl": round(total_pnl / MILLI, 3),
            "max_drawdown": round(max_dd / MILLI, 3),
        },
        "breakdown": breakdown,
        "equity_curve": [
            {"ts_ms": ts, "equity": round(v / MILLI, 3)} for ts, v in equity
        ],
    }
