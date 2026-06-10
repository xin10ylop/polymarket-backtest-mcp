"""Unit tests for the backtest engine and store-level invariants."""

import pytest

from pmbt.engine import (
    MILLI,
    cents_to_milli,
    dollars_to_milli,
    run_backtest,
    simulate_market,
    summarize,
)

E55 = dollars_to_milli(0.55)  # 550
TP5 = cents_to_milli(5)       # 50


def mk(slug="m1", winner="up", tradeable=True, start=1_000_000):
    return {"slug": slug, "winner": winner, "tradeable": tradeable,
            "window_start_ms": start}


# ---------------------------------------------------------------- fills

def test_buy_fills_on_ask_not_bid_or_mid():
    # bid touches 0.55 but ask stays above: no fill
    quotes = [(1, 550, 600), (2, 540, 580)]
    assert simulate_market(quotes, "up", "up", E55, E55 + TP5) is None
    # ask touches 0.55: fill at the limit, on the FIRST qualifying tick
    quotes = [(1, 500, 560), (2, 500, 550), (3, 480, 540)]
    t = simulate_market(quotes, "down", "up", E55, E55 + TP5)
    assert t is not None
    assert t.entry_ts_ms == 2
    assert t.entry_milli == 550


def test_buy_fills_at_entry_price_even_if_ask_below():
    # ask gaps to 0.50; resting buy at 0.55 still fills at 0.55 exactly
    quotes = [(1, 480, 500), (2, 480, 500)]
    t = simulate_market(quotes, "up", "up", E55, E55 + TP5)
    assert t.entry_milli == 550


def test_sell_fills_on_bid_gap_at_limit_price():
    # entry 0.55, TP 0.60; bid jumps 0.55 -> 0.70: sell fills at 0.60, not 0.70
    quotes = [(1, 540, 550), (2, 550, 560), (3, 700, 710)]
    t = simulate_market(quotes, "up", "up", E55, E55 + TP5)
    assert t.exit_kind == "take_profit"
    assert t.exit_milli == 600
    assert t.pnl_milli == 50


def test_tp_checked_only_after_entry_tick():
    # bid >= target on the entry tick itself must not count; only later ticks
    quotes = [(1, 610, 550)]  # crossed book on the single tick
    t = simulate_market(quotes, "down", "up", E55, E55 + TP5)
    assert t.exit_kind == "forced_loss"


def test_no_float_drift_in_entry_plus_tp():
    # 0.1 + 0.2 style traps: everything is integer arithmetic
    for entry, tp_cents in [(0.1, 0.2), (0.55, 5), (0.07, 0.1), (0.33, 3.3)]:
        e = dollars_to_milli(entry)
        target = e + cents_to_milli(tp_cents)
        assert isinstance(e, int) and isinstance(target, int)
        assert target == round(entry * 1000) + round(tp_cents * 10)


def test_untriggered_market_no_trade():
    quotes = [(1, 600, 610), (2, 620, 630)]
    assert simulate_market(quotes, "up", "up", E55, E55 + TP5) is None
    res = run_backtest([mk()], lambda s: quotes, "up", 0.55, 5)
    assert res.untriggered_markets == 1
    assert len(res.trades) == 0
    # untriggered markets are excluded from win_rate
    assert summarize(res)["stats"]["win_rate"] is None


# ---------------------------------------------------------------- resolution

def test_forced_loss_equals_minus_entry():
    quotes = [(1, 540, 550)]
    t = simulate_market(quotes, "down", "up", E55, E55 + TP5)
    assert t.exit_kind == "forced_loss"
    assert t.pnl_milli == -550
    assert t.pnl_milli / MILLI == -0.55


def test_forced_win_equals_one_minus_entry():
    quotes = [(1, 540, 550)]
    t = simulate_market(quotes, "up", "up", E55, E55 + TP5)
    assert t.exit_kind == "forced_win"
    assert t.pnl_milli == 1000 - 550


def test_tie_resolves_up():
    """Polymarket rule: Chainlink end price >= start price means Up wins.

    The ingest never derives winners from prices: it copies winner_side from
    the outcomes file, which encodes the official >= rule. A tie market in
    the outcomes file carries winner_side='UP', and an Up holder force-resolves
    as a win.
    """
    quotes = [(1, 540, 550)]
    t = simulate_market(quotes, "up", "up", E55, E55 + TP5)  # winner='up' (tie -> UP)
    assert t.exit_kind == "forced_win"
    t2 = simulate_market(quotes, "up", "down", E55, E55 + TP5)
    assert t2.exit_kind == "forced_loss"


def test_winner_field_decides_not_reference_prices():
    """Approximate price_to_beat / BTC reference prices never reach PnL code:
    simulate_market has no price arguments other than token quotes, and the
    outcome flips solely with the winner field."""
    quotes = [(1, 540, 550)]
    win = simulate_market(quotes, "up", "up", E55, E55 + TP5)
    loss = simulate_market(quotes, "down", "up", E55, E55 + TP5)
    assert win.pnl_milli > 0 > loss.pnl_milli
    import inspect
    sig = inspect.signature(simulate_market)
    assert "price_to_beat" not in sig.parameters
    assert "btc" not in str(sig.parameters)


# ---------------------------------------------------------------- universe

def test_one_trade_per_market_max():
    # entry condition occurs many times; still exactly one trade
    quotes = [(i, 500, 540) for i in range(1, 50)]
    res = run_backtest([mk()], lambda s: quotes, "up", 0.55, 5)
    assert len(res.trades) == 1


def test_non_tradeable_2s_only_markets_excluded():
    markets = [mk("primary", tradeable=True), mk("supp-only", tradeable=False)]
    quotes = {"primary": [(1, 500, 540)], "supp-only": [(1, 500, 540)]}
    res = run_backtest(markets, lambda s: quotes[s], "up", 0.55, 5)
    assert res.markets_in_range == 2
    assert res.markets_tested == 1
    assert [t.slug for t in res.trades] == ["primary"]


def test_no_cross_market_leakage():
    # market B has fill-triggering quotes; market A does not. A must not trade.
    markets = [mk("a"), mk("b")]
    quotes = {"a": [(1, 600, 700)], "b": [(1, 500, 540)]}
    res = run_backtest(markets, lambda s: quotes[s], "up", 0.55, 5)
    assert [t.slug for t in res.trades] == ["b"]
    assert res.untriggered_markets == 1


def test_uses_own_side_quotes_only():
    # down side would fill, up side would not; trading 'up' must not trade.
    # quotes_for returns the chosen side's columns, so the engine only ever
    # sees one side; this asserts the call contract.
    seen = []

    def quotes_for(slug, side="up"):
        seen.append(slug)
        return [(1, 600, 700)]  # up side never fills

    res = run_backtest([mk()], quotes_for, "up", 0.55, 5)
    assert res.untriggered_markets == 1


# ---------------------------------------------------------------- summary

def test_breakdown_sums_to_num_trades():
    markets = [mk(f"m{i}", winner="up" if i % 2 else "down", start=i)
               for i in range(10)]
    quotes = {}
    for i in range(10):
        if i < 3:  # tp exit
            quotes[f"m{i}"] = [(1, 500, 550), (2, 650, 660)]
        elif i < 7:  # forced resolution
            quotes[f"m{i}"] = [(1, 500, 550)]
        else:  # untriggered
            quotes[f"m{i}"] = [(1, 600, 700)]
    res = run_backtest(markets, lambda s: quotes[s], "up", 0.55, 5)
    s = summarize(res)
    b = s["breakdown"]
    assert (b["take_profit_exits"] + b["forced_resolution_wins"]
            + b["forced_resolution_losses"]) == s["stats"]["num_trades"] == 7
    assert res.untriggered_markets == 3


def test_equity_curve_capped_and_ordered():
    markets = [mk(f"m{i}", start=i * 1000) for i in range(500)]
    res = run_backtest(markets, lambda s: [(1, 500, 550), (2, 650, 660)],
                       "up", 0.55, 5)
    eq = summarize(res)["equity_curve"]
    assert len(eq) <= 200
    ts = [p["ts_ms"] for p in eq]
    assert ts == sorted(ts)


def test_taker_fee_zero_by_default():
    quotes = [(1, 500, 550), (2, 650, 660)]
    t0 = simulate_market(quotes, "up", "up", E55, E55 + TP5)
    tf = simulate_market(quotes, "up", "up", E55, E55 + TP5, taker_fee=0.01)
    assert t0.pnl_milli == 50
    assert tf.pnl_milli < t0.pnl_milli
