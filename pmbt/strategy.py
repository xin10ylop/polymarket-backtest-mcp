"""Composable condition-based strategy engine ("bricks").

Strategies are structured data validated against a fixed vocabulary; no
user-supplied code or expressions are ever evaluated. Every brick preserves
the audited v1 guarantees: correct fill side, maker/taker semantics,
integer tenths-of-a-cent fill arithmetic, one trade per market, no
look-ahead, winner-only resolution.

Look-ahead safety by construction: each market's ticks are processed in
time order; condition evaluation at tick i reads only ticks <= i (trailing
pointers move forward only); the entry strictly precedes the exit scan; the
winner is consulted only after the last tick (i.e. after window_end).

Fill conventions:
  maker (limit entry, take-profit): fills AT THE LIMIT PRICE on first touch
      of the opposing best quote, fee 0.
  taker (market entry, stop-loss, time exit): fills at the OBSERVED quote of
      the triggering tick, so gaps work AGAINST the trader; taker_fee applies.
Same-tick exit priority: stop_loss, then take_profit, then time exit.
Exits are evaluated on ticks strictly after the entry fill tick.
"""

from bisect import bisect_right
from dataclasses import dataclass, field

from .engine import MILLI, cents_to_milli, dollars_to_milli

OPS = ("<=", ">=")

ASSUMPTIONS = [
    "Maker fills (limit entry, take-profit) execute at the limit price the "
    "first time the opposing best quote touches it; maker fee is 0.",
    "Taker fills (market entry, stop-loss, time exit) execute at the OBSERVED "
    "best quote of the triggering tick, so price gaps work against the trader.",
    "taker_fee is a flat fraction of notional applied to taker fills only. "
    "Real Polymarket taker fees are odds-dependent (largest near 50c, roughly "
    "up to ~1.8%); the flat rate is an approximation, not the exact formula.",
    "Same-tick exit priority: stop_loss before take_profit before time exit "
    "(conservative). Exits are evaluated on ticks strictly after the entry "
    "fill tick.",
    "Entry conditions are evaluated at tick timestamps and must all hold "
    "simultaneously at one tick; trailing windows require enough history.",
    "If no exit triggers before window_end, the position force-resolves by "
    "the official winner: $1.00 if the held side won, $0.00 if it lost. "
    "No fee on redemption.",
    "One trade per market maximum; static historical dataset.",
]

VOCABULARY = {
    "side": {"values": ["up", "down"], "description": "Token side to trade."},
    "entry.conditions": {
        "semantics": "ALL conditions must hold simultaneously at one tick "
                     "(logical AND). An empty list triggers at the market's "
                     "first tick.",
        "types": {
            "price_level": {
                "fields": {"op": "'<=' or '>='", "value_cents": "number, 0-100 exclusive"},
                "description": "The chosen side's quote vs a level. op '<=' "
                               "uses the side's ASK (cheap enough to buy); "
                               "op '>=' uses the side's BID.",
                "example": {"type": "price_level", "op": "<=", "value_cents": 30},
            },
            "price_move": {
                "fields": {"window_seconds": "int, 1-3600",
                           "op": "'<=' or '>='",
                           "value": "number (may be negative)",
                           "unit": "'cents' or 'percent'"},
                "description": "Change in the side's mid over the trailing "
                               "window, computed only from ticks at or before "
                               "the current tick. The reference is the last "
                               "known mid at or before (now - window); with "
                               "less history than the window the condition "
                               "is false.",
                "example": {"type": "price_move", "window_seconds": 30,
                            "op": ">=", "value": 5, "unit": "percent"},
            },
            "time_to_close": {
                "fields": {"op": "'<=' or '>='", "seconds": "int >= 0"},
                "description": "Seconds remaining until window_end.",
                "example": {"type": "time_to_close", "op": "<=", "seconds": 90},
            },
            "btc_move": {
                "fields": {"op": "'<=' or '>='", "value": "number (may be negative)",
                           "unit": "'dollars' or 'percent'"},
                "description": "BTC reference price now vs the market's first "
                               "available reference tick. Display-grade "
                               "exchange feed: usable as a SIGNAL, never used "
                               "for resolution. This brick compares float "
                               "dollar values (the feed is float by nature); "
                               "all fill and PnL arithmetic stays integer "
                               "tenths-of-a-cent.",
                "example": {"type": "btc_move", "op": ">=", "value": 0.05,
                            "unit": "percent"},
            },
        },
    },
    "entry.execution": {
        "limit": {
            "fields": {"style": "'limit'", "limit_price_cents": "number, 0-100 exclusive"},
            "description": "From the first tick where conditions hold, rest a "
                           "limit buy; fills at the first tick (possibly the "
                           "trigger tick itself) where best_ask <= limit, AT "
                           "the limit price (maker, fee 0). May never fill -> "
                           "untriggered.",
        },
        "market": {
            "fields": {"style": "'market'"},
            "description": "Buy at the triggering tick's OBSERVED best_ask "
                           "(taker; taker_fee applies; gaps work against the "
                           "buyer). A market buy needs a live ask, so ticks "
                           "with no ask are skipped: the entry happens at the "
                           "first tick where the conditions hold AND an ask "
                           "exists.",
        },
    },
    "exit": {
        "take_profit_cents": "int > 0 or null: resting limit sell at "
                             "fill + Y cents; fills on best_bid >= target AT "
                             "the limit price even on gaps (maker, fee 0).",
        "stop_loss_cents": "int > 0 or null: market sell when best_bid <= "
                           "fill - Z cents; fills at the OBSERVED bid, so a "
                           "gap through the stop fills at the bad price, not "
                           "the stop level (taker; fee applies).",
        "exit_seconds_before_close": "int > 0 or null: market sell at the "
                                     "last tick at/before window_end - T "
                                     "seconds, at observed best_bid (taker; "
                                     "fee applies). If entry happens later "
                                     "than that tick, the sell executes at "
                                     "the first tick after entry.",
        "default": "If nothing triggers, hold to resolution and redeem "
                   "$1.00/$0.00 by the official winner.",
    },
    "examples": [
        {
            "name": "Late cheap Down, hold to resolution",
            "prompt": "buy Down at market when it's under 30c with under 90s "
                      "left, hold to resolution",
            "strategy": {
                "side": "down",
                "entry": {
                    "conditions": [
                        {"type": "price_level", "op": "<=", "value_cents": 30},
                        {"type": "time_to_close", "op": "<=", "seconds": 90},
                    ],
                    "execution": {"style": "market"},
                },
                "exit": {},
            },
        },
        {
            "name": "Momentum Up with bracket",
            "prompt": "buy Up at market on a 5%-in-30s upward move, 3c stop, "
                      "5c take-profit",
            "strategy": {
                "side": "up",
                "entry": {
                    "conditions": [
                        {"type": "price_move", "window_seconds": 30,
                         "op": ">=", "value": 5, "unit": "percent"},
                    ],
                    "execution": {"style": "market"},
                },
                "exit": {"take_profit_cents": 5, "stop_loss_cents": 3},
            },
        },
        {
            "name": "The original v1 strategy in brick form",
            "prompt": "rest a limit buy on Up at 40c, take profit 5c, hold to "
                      "resolution otherwise",
            "strategy": {
                "side": "up",
                "entry": {
                    "conditions": [],
                    "execution": {"style": "limit", "limit_price_cents": 40},
                },
                "exit": {"take_profit_cents": 5},
            },
        },
    ],
}


# ------------------------------------------------------------------ schema

def _err(msg: str) -> ValueError:
    return ValueError(
        f"{msg}. Valid vocabulary: condition types "
        f"{sorted(VOCABULARY['entry.conditions']['types'])}, execution styles "
        f"['limit', 'market'], exit keys ['take_profit_cents', "
        f"'stop_loss_cents', 'exit_seconds_before_close']. "
        f"Call strategy_vocabulary() for full schemas and examples."
    )


def _num(d, key, lo=None, hi=None, allow_neg=False):
    v = d.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise _err(f"'{key}' must be a number, got {v!r}")
    if not allow_neg and lo is None and v <= 0:
        raise _err(f"'{key}' must be positive, got {v!r}")
    if lo is not None and v < lo:
        raise _err(f"'{key}' must be >= {lo}, got {v!r}")
    if hi is not None and v > hi:
        raise _err(f"'{key}' must be <= {hi}, got {v!r}")
    return v


def _check_keys(d, allowed, ctx):
    extra = set(d) - set(allowed)
    if extra:
        raise _err(f"unknown field(s) {sorted(extra)} in {ctx}")


@dataclass
class Strategy:
    side: str
    conditions: list                 # validated raw condition dicts
    exec_style: str                  # 'limit' | 'market'
    exec_limit_milli: int | None     # for style 'limit'
    tp_delta_milli: int | None
    stop_delta_milli: int | None
    time_exit_s: int | None
    canonical: dict = field(default_factory=dict)


def validate_strategy(raw: dict) -> Strategy:
    """Validate a strategy dict against the brick vocabulary. Raises
    ValueError with a vocabulary hint on any malformed or unknown field."""
    if not isinstance(raw, dict):
        raise _err(f"strategy must be an object, got {type(raw).__name__}")
    _check_keys(raw, {"side", "entry", "exit"}, "strategy")
    side = raw.get("side")
    if side not in ("up", "down"):
        raise _err(f"side must be 'up' or 'down', got {side!r}")

    entry = raw.get("entry")
    if not isinstance(entry, dict):
        raise _err("entry must be an object with 'conditions' and 'execution'")
    _check_keys(entry, {"conditions", "execution"}, "entry")
    conds = entry.get("conditions", [])
    if not isinstance(conds, list):
        raise _err("entry.conditions must be a list")
    for c in conds:
        if not isinstance(c, dict):
            raise _err("each condition must be an object")
        t = c.get("type")
        if t == "price_level":
            _check_keys(c, {"type", "op", "value_cents"}, "price_level")
            if c.get("op") not in OPS:
                raise _err(f"price_level.op must be one of {OPS}")
            _num(c, "value_cents", lo=0.1, hi=99.9)
        elif t == "price_move":
            _check_keys(c, {"type", "window_seconds", "op", "value", "unit"},
                        "price_move")
            if c.get("op") not in OPS:
                raise _err(f"price_move.op must be one of {OPS}")
            if c.get("unit") not in ("cents", "percent"):
                raise _err("price_move.unit must be 'cents' or 'percent'")
            _num(c, "window_seconds", lo=1, hi=3600)
            _num(c, "value", allow_neg=True, lo=-10000, hi=10000)
        elif t == "time_to_close":
            _check_keys(c, {"type", "op", "seconds"}, "time_to_close")
            if c.get("op") not in OPS:
                raise _err(f"time_to_close.op must be one of {OPS}")
            _num(c, "seconds", lo=0, hi=86400)
        elif t == "btc_move":
            _check_keys(c, {"type", "op", "value", "unit"}, "btc_move")
            if c.get("op") not in OPS:
                raise _err(f"btc_move.op must be one of {OPS}")
            if c.get("unit") not in ("dollars", "percent"):
                raise _err("btc_move.unit must be 'dollars' or 'percent'")
            _num(c, "value", allow_neg=True, lo=-1e9, hi=1e9)
        else:
            raise _err(f"unknown condition type {t!r}")

    execution = entry.get("execution")
    if not isinstance(execution, dict):
        raise _err("entry.execution must be an object with a 'style'")
    style = execution.get("style")
    limit_milli = None
    if style == "limit":
        _check_keys(execution, {"style", "limit_price_cents"}, "execution")
        limit_milli = cents_to_milli(_num(execution, "limit_price_cents",
                                          lo=0.1, hi=99.9))
    elif style == "market":
        _check_keys(execution, {"style"}, "execution")
    else:
        raise _err(f"execution.style must be 'limit' or 'market', got {style!r}")

    exit_ = raw.get("exit", {}) or {}
    if not isinstance(exit_, dict):
        raise _err("exit must be an object")
    _check_keys(exit_, {"take_profit_cents", "stop_loss_cents",
                        "exit_seconds_before_close"}, "exit")
    tp = exit_.get("take_profit_cents")
    sl = exit_.get("stop_loss_cents")
    te = exit_.get("exit_seconds_before_close")
    tp_milli = cents_to_milli(_num(exit_, "take_profit_cents", lo=0.1, hi=99.9)) \
        if tp is not None else None
    sl_milli = cents_to_milli(_num(exit_, "stop_loss_cents", lo=0.1, hi=99.9)) \
        if sl is not None else None
    if te is not None:
        te = int(_num(exit_, "exit_seconds_before_close", lo=1, hi=86400))

    canonical = {
        "side": side,
        "entry": {"conditions": conds,
                  "execution": dict(execution)},
        "exit": {"take_profit_cents": tp, "stop_loss_cents": sl,
                 "exit_seconds_before_close": te},
    }
    return Strategy(side=side, conditions=conds, exec_style=style,
                    exec_limit_milli=limit_milli, tp_delta_milli=tp_milli,
                    stop_delta_milli=sl_milli, time_exit_s=te,
                    canonical=canonical)


def legacy_strategy(side: str, entry_price: float, take_profit_cents: float) -> Strategy:
    """Map the v1 flat parameters onto the brick engine: no conditions,
    limit entry at entry_price, take-profit exit. Must reproduce the v1
    engine exactly (regression-anchored test)."""
    if side not in ("up", "down"):
        raise ValueError("side must be 'up' or 'down'")
    if not (0 < entry_price < 1):
        raise ValueError("entry_price must be between 0 and 1 (dollars)")
    if take_profit_cents <= 0:
        raise ValueError("take_profit_cents must be positive")
    entry_milli = dollars_to_milli(entry_price)
    return Strategy(
        side=side, conditions=[], exec_style="limit",
        exec_limit_milli=entry_milli,
        tp_delta_milli=cents_to_milli(take_profit_cents),
        stop_delta_milli=None, time_exit_s=None,
        canonical={
            "side": side,
            "entry": {"conditions": [],
                      "execution": {"style": "limit",
                                    "limit_price_cents": entry_milli / 10}},
            "exit": {"take_profit_cents": take_profit_cents,
                     "stop_loss_cents": None,
                     "exit_seconds_before_close": None},
        },
    )


# ------------------------------------------------------------- evaluation

def _compile_conditions(conds, ts, bid, ask, btc, window_end_ms):
    """Build per-market condition checks. Every check at index i reads only
    arrays[0..i]; trailing pointers advance monotonically (no look-ahead)."""
    checks = []
    mid2 = None
    btc_ffill = None
    for c in conds:
        t = c["type"]
        if t == "price_level":
            level = cents_to_milli(c["value_cents"])
            if c["op"] == "<=":
                # cheap enough to buy: the side's ASK at or under the level
                checks.append(lambda i, lv=level: 0 <= ask[i] <= lv)
            else:
                # rich side: the side's BID at or over the level
                checks.append(lambda i, lv=level: bid[i] >= lv)
        elif t == "time_to_close":
            lim_ms = int(c["seconds"]) * 1000
            if c["op"] == "<=":
                checks.append(lambda i, lm=lim_ms: window_end_ms - ts[i] <= lm)
            else:
                checks.append(lambda i, lm=lim_ms: window_end_ms - ts[i] >= lm)
        elif t == "price_move":
            if mid2 is None:
                # mid in HALF tenths-of-a-cent (bid+ask), exact integers
                mid2 = [b + a if (b >= 0 and a >= 0) else None
                        for b, a in zip(bid, ask)]
            w_ms = int(c["window_seconds"]) * 1000
            ge = c["op"] == ">="
            if c["unit"] == "cents":
                thr2 = round(c["value"] * 20)  # cents -> mid2 units (2x milli)

                def chk(i, w=w_ms, th=thr2, st={"j": 0}):
                    tgt = ts[i] - w
                    if ts[0] > tgt:
                        return False  # not enough trailing history
                    j = st["j"]
                    while j + 1 <= i and ts[j + 1] <= tgt:
                        j += 1
                    st["j"] = j
                    if mid2[j] is None or mid2[i] is None:
                        return False
                    d = mid2[i] - mid2[j]
                    return d >= th if ge else d <= th
            else:  # percent, integer cross-multiplication: d*100 vs v*ref
                val = c["value"]

                def chk(i, w=w_ms, v=val, st={"j": 0}):
                    tgt = ts[i] - w
                    if ts[0] > tgt:
                        return False
                    j = st["j"]
                    while j + 1 <= i and ts[j + 1] <= tgt:
                        j += 1
                    st["j"] = j
                    if mid2[j] is None or mid2[i] is None or mid2[j] <= 0:
                        return False
                    lhs = (mid2[i] - mid2[j]) * 100
                    rhs = v * mid2[j]
                    return lhs >= rhs if ge else lhs <= rhs
            checks.append(chk)
        elif t == "btc_move":
            if btc_ffill is None:
                btc_ffill, last = [], None
                for v in btc:
                    if v is not None:
                        last = v
                    btc_ffill.append(last)
            # baseline = the first non-null reference in the market. Its
            # index can be later than early ticks, but that cannot leak the
            # future: until that tick, btc_ffill[i] is None and the check
            # below returns False, so no decision depends on the value.
            baseline = next((v for v in btc if v is not None), None)
            val, ge, pct = c["value"], c["op"] == ">=", c["unit"] == "percent"

            def bchk(i, base=baseline, v=val, g=ge, p=pct):
                cur = btc_ffill[i]
                if base is None or cur is None or (p and base == 0):
                    return False
                d = (cur - base) / base * 100 if p else cur - base
                return d >= v if g else d <= v
            checks.append(bchk)
    return checks


@dataclass
class STrade:
    slug: str
    window_start_ms: int
    entry_ts_ms: int
    entry_milli: int       # actual fill price
    entry_taker: bool
    trigger_mid2: int | None  # (bid+ask) at the trigger tick, taker entries
    exit_kind: str  # take_profit | stop_loss | time_exit | forced_win | forced_loss
    exit_milli: int
    fees_milli: int
    pnl_milli: int


def simulate_market(quotes, window_end_ms, winner, strat: Strategy,
                    taker_fee: float = 0.0):
    """Simulate one market under a validated Strategy.

    quotes: list of (ts_ms, bid_milli, ask_milli, btc_ref) for the CHOSEN
            side, in time order, in-window only; -1 marks a missing quote.
    winner is consulted only after the tick scan ends (post window_end).
    Returns an STrade or None (untriggered).
    """
    n = len(quotes)
    if n == 0:
        return None
    ts = [q[0] for q in quotes]
    bid = [q[1] for q in quotes]
    ask = [q[2] for q in quotes]
    btc = [q[3] for q in quotes]

    checks = _compile_conditions(strat.conditions, ts, bid, ask, btc,
                                 window_end_ms)
    need_ask = strat.exec_style == "market"

    # ---- entry: first tick where ALL conditions hold simultaneously
    trigger_i = None
    for i in range(n):
        if need_ask and ask[i] < 0:
            continue  # a market buy needs a live ask at the trigger tick
        ok = True
        for chk in checks:
            if not chk(i):
                ok = False
                break
        if ok:
            trigger_i = i
            break
    if trigger_i is None:
        return None

    fees = 0
    if strat.exec_style == "market":
        fill_i = trigger_i
        fill = ask[trigger_i]  # observed ask: gaps work against the buyer
        trigger_mid2 = (bid[trigger_i] + ask[trigger_i]
                        if bid[trigger_i] >= 0 else None)
        if taker_fee:
            fees += int(round(taker_fee * fill))
        entry_taker = True
    else:
        lim = strat.exec_limit_milli
        fill_i = None
        for i in range(trigger_i, n):  # may fill on the trigger tick itself
            if 0 <= ask[i] <= lim:
                fill_i = i
                break
        if fill_i is None:
            return None  # resting limit never touched -> untriggered
        fill = lim  # maker fills at the limit price exactly
        trigger_mid2 = None
        entry_taker = False

    # ---- exits: ticks strictly after the entry fill tick.
    tp_level = fill + strat.tp_delta_milli if strat.tp_delta_milli else None
    stop_level = fill - strat.stop_delta_milli if strat.stop_delta_milli else None
    time_due = False
    sched_i = None
    if strat.time_exit_s is not None:
        thresh = window_end_ms - strat.time_exit_s * 1000
        # the sell is scheduled at entry time for a fixed wall-clock time;
        # executing on the last tick at/before it uses no future information
        sched_i = bisect_right(ts, thresh) - 1

    exit_kind = exit_milli = None
    for i in range(fill_i + 1, n):
        b = bid[i]
        # priority: stop_loss > take_profit > time exit (conservative)
        if stop_level is not None and 0 <= b <= stop_level:
            exit_kind, exit_milli = "stop_loss", b  # observed bid, gap = bad fill
            if taker_fee:
                fees += int(round(taker_fee * b))
            break
        if tp_level is not None and b >= tp_level:
            exit_kind, exit_milli = "take_profit", tp_level  # maker, at limit
            break
        if sched_i is not None and (i >= sched_i or time_due):
            if b >= 0:
                exit_kind, exit_milli = "time_exit", b  # observed bid, taker
                if taker_fee:
                    fees += int(round(taker_fee * b))
                break
            time_due = True  # market order stays working until a bid exists

    if exit_kind is None:  # resolution: only consulted after the tick scan
        if winner == strat.side:
            exit_kind, exit_milli = "forced_win", MILLI
        else:
            exit_kind, exit_milli = "forced_loss", 0

    pnl = exit_milli - fill - fees
    return STrade(
        slug="", window_start_ms=0, entry_ts_ms=ts[fill_i],
        entry_milli=fill, entry_taker=entry_taker, trigger_mid2=trigger_mid2,
        exit_kind=exit_kind, exit_milli=exit_milli, fees_milli=fees,
        pnl_milli=pnl,
    )


@dataclass
class StrategyResult:
    trades: list = field(default_factory=list)
    markets_in_range: int = 0
    markets_tested: int = 0
    untriggered_markets: int = 0


def run_backtest(markets, quotes_for, strat: Strategy, taker_fee: float = 0.0):
    """markets: dicts with slug, window_start_ms, window_end_ms, winner,
    tradeable. quotes_for: slug -> list of (ts_ms, bid, ask, btc_ref) for the
    strategy's side."""
    res = StrategyResult(markets_in_range=len(markets))
    for m in markets:
        if not m["tradeable"]:
            continue
        res.markets_tested += 1
        trade = simulate_market(
            quotes_for(m["slug"]), m["window_end_ms"], m["winner"], strat,
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


def summarize(res: StrategyResult, max_equity_points: int = 200):
    trades = res.trades
    n = len(trades)
    wins = [t for t in trades if t.pnl_milli > 0]
    losses = [t for t in trades if t.pnl_milli <= 0]
    total_pnl = sum(t.pnl_milli for t in trades)

    equity, peak, max_dd, cum = [], 0, 0, 0
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

    kinds = {}
    for t in trades:
        kinds[t.exit_kind] = kinds.get(t.exit_kind, 0) + 1
    breakdown = {
        "take_profit_exits": kinds.get("take_profit", 0),
        "stop_loss_exits": kinds.get("stop_loss", 0),
        "time_exits": kinds.get("time_exit", 0),
        "forced_resolution_wins": kinds.get("forced_win", 0),
        "forced_resolution_losses": kinds.get("forced_loss", 0),
    }

    takers = [t for t in trades if t.entry_taker]
    slips = [(t.entry_milli * 2 - t.trigger_mid2) / 20 for t in takers
             if t.trigger_mid2 is not None]
    entry_fill_stats = {
        "maker_entries": n - len(takers),
        "taker_entries": len(takers),
        "avg_fill_price": round(sum(t.entry_milli for t in trades) / n / MILLI, 4)
        if n else None,
        "avg_market_entry_slippage_cents": round(sum(slips) / len(slips), 3)
        if slips else None,
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
            "total_fees_paid": round(sum(t.fees_milli for t in trades) / MILLI, 3),
        },
        "breakdown": breakdown,
        "entry_fill_stats": entry_fill_stats,
        "equity_curve": [
            {"ts_ms": ts_, "equity": round(v / MILLI, 3)} for ts_, v in equity
        ],
    }
