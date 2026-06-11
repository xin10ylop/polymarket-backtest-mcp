"""Tests for the composable strategy engine (bricks)."""

import copy
import os

import pytest

from pmbt import engine as v1
from pmbt.strategy import (
    Strategy,
    legacy_strategy,
    run_backtest,
    simulate_market,
    summarize,
    validate_strategy,
)

WE = 1_300_000  # window_end_ms for synthetic markets (start at 1_000_000)


def q(ts, bid, ask, btc=None):
    return (ts, bid, ask, btc)


def strat(side="up", conditions=None, execution=None, exit=None):
    return validate_strategy({
        "side": side,
        "entry": {"conditions": conditions or [],
                  "execution": execution or {"style": "market"}},
        "exit": exit or {},
    })


def mk(slug="m1", winner="up", tradeable=True, start=1_000_000, end=WE):
    return {"slug": slug, "winner": winner, "tradeable": tradeable,
            "window_start_ms": start, "window_end_ms": end}


# ------------------------------------------------------------- conditions

def test_price_level_le_uses_ask_not_bid():
    s = strat(conditions=[{"type": "price_level", "op": "<=", "value_cents": 30}])
    # bid under 30c but ask above: must NOT trigger
    t = simulate_market([q(1_000_100, 250, 320), q(1_000_200, 250, 320)],
                        WE, "down", s)
    assert t is None
    # ask touches 30c: triggers there, market buy at observed ask
    t = simulate_market([q(1_000_100, 250, 320), q(1_000_200, 280, 300)],
                        WE, "down", s)
    assert t.entry_ts_ms == 1_000_200
    assert t.entry_milli == 300


def test_price_level_ge_uses_bid_not_ask():
    s = strat(conditions=[{"type": "price_level", "op": ">=", "value_cents": 60}])
    # ask over 60c but bid below: must NOT trigger
    t = simulate_market([q(1_000_100, 550, 650)], WE, "down", s)
    assert t is None
    t = simulate_market([q(1_000_100, 550, 650), q(1_000_200, 600, 650)],
                        WE, "down", s)
    assert t is not None
    assert t.entry_ts_ms == 1_000_200


def test_price_move_cents_trailing_window():
    s = strat(conditions=[{"type": "price_move", "window_seconds": 10,
                           "op": ">=", "value": 5, "unit": "cents"}])
    quotes = [
        q(1_000_000, 400, 410),   # mid 40.5c
        q(1_005_000, 420, 430),   # +2c in 5s, but <10s of history exists
        q(1_011_000, 450, 460),   # ref = tick0 (11s back): +5.0c -> trigger
    ]
    t = simulate_market(quotes, WE, "up", s)
    assert t is not None
    assert t.entry_ts_ms == 1_011_000
    # insufficient history alone never triggers
    t = simulate_market(quotes[:2], WE, "up", s)
    assert t is None


def test_price_move_uses_last_known_mid_at_window_start():
    # ref must be the LAST tick at/before (now - window), not the first ever:
    # under quote-change dedup the last stored tick IS the true mid there
    s = strat(conditions=[{"type": "price_move", "window_seconds": 10,
                           "op": ">=", "value": 5, "unit": "cents"}])
    quotes = [
        q(1_000_000, 390, 400),   # mid 39.5c
        q(1_020_000, 400, 410),   # +1c over 20s: no trigger at this tick
        q(1_031_000, 440, 450),   # ref is tick1 (last <= t-10s): +4c -> no
        q(1_032_000, 450, 460),   # vs tick1: +5c -> trigger
    ]
    t = simulate_market(quotes, WE, "up", s)
    assert t.entry_ts_ms == 1_032_000
    # if the first-ever tick were (wrongly) used as the reference, tick 2
    # would already show +4.5c... make that explicit: vs tick0 the move at
    # tick2 is +5.0c, so a first-tick reference would trigger one tick early
    assert (450 + 440) - (390 + 400) == 100  # +5.0c vs tick0
    assert t.entry_ts_ms != 1_031_000


def test_price_move_percent():
    s = strat(conditions=[{"type": "price_move", "window_seconds": 30,
                           "op": ">=", "value": 5, "unit": "percent"}])
    quotes = [
        q(1_000_000, 400, 400),            # mid 40c
        q(1_040_000, 415, 425),            # mid 42c = +5.0% -> trigger
    ]
    t = simulate_market(quotes, WE, "up", s)
    assert t.entry_ts_ms == 1_040_000
    # 4.9% must not trigger (integer cross-multiplication, no float fuzz)
    quotes[1] = q(1_040_000, 414, 424)
    assert simulate_market(quotes, WE, "up", s) is None


def test_time_to_close():
    s = strat(conditions=[{"type": "time_to_close", "op": "<=", "seconds": 90}])
    quotes = [q(WE - 91_000, 300, 310), q(WE - 90_000, 300, 310)]
    t = simulate_market(quotes, WE, "up", s)
    assert t.entry_ts_ms == WE - 90_000
    s = strat(conditions=[{"type": "time_to_close", "op": ">=", "seconds": 91}])
    t = simulate_market(quotes, WE, "up", s)
    assert t.entry_ts_ms == WE - 91_000


def test_btc_move_dollars_and_percent_baseline_first_tick():
    s = strat(conditions=[{"type": "btc_move", "op": ">=", "value": 50,
                           "unit": "dollars"}])
    quotes = [
        q(1_000_100, 400, 410, None),      # null btc: baseline comes later
        q(1_000_200, 400, 410, 70_000.0),  # baseline
        q(1_000_300, 400, 410, 70_049.0),  # +49 -> no
        q(1_000_400, 400, 410, 70_050.0),  # +50 -> trigger
    ]
    t = simulate_market(quotes, WE, "up", s)
    assert t.entry_ts_ms == 1_000_400
    s = strat(conditions=[{"type": "btc_move", "op": "<=", "value": -0.1,
                           "unit": "percent"}])
    quotes = [
        q(1_000_100, 400, 410, 70_000.0),
        q(1_000_200, 400, 410, 69_930.0),  # -0.1% exactly -> trigger
    ]
    t = simulate_market(quotes, WE, "up", s)
    assert t.entry_ts_ms == 1_000_200


def test_and_semantics_simultaneous():
    conds = [
        {"type": "price_level", "op": "<=", "value_cents": 30},
        {"type": "time_to_close", "op": "<=", "seconds": 90},
    ]
    s = strat(conditions=conds)
    # cheap early, late but expensive: each condition true at a different
    # tick only -> never triggers
    quotes = [q(WE - 200_000, 250, 290), q(WE - 50_000, 350, 360)]
    assert simulate_market(quotes, WE, "down", s) is None
    # both true at one tick -> triggers
    quotes.append(q(WE - 40_000, 250, 290))
    t = simulate_market(quotes, WE, "down", s)
    assert t.entry_ts_ms == WE - 40_000


def test_empty_conditions_trigger_first_tick():
    s = strat(execution={"style": "market"})
    t = simulate_market([q(1_000_100, 400, 410)], WE, "up", s)
    assert t.entry_ts_ms == 1_000_100
    assert t.entry_milli == 410


# ------------------------------------------------------------- executions

def test_market_entry_fills_at_observed_ask_gap():
    # ask gaps 0.40 -> 0.48 while a level condition waits at <= 45:
    # the buyer pays the observed 48? No: condition is on the ask itself,
    # so use a time condition to decouple trigger from price.
    s = strat(conditions=[{"type": "time_to_close", "op": "<=", "seconds": 100}],
              execution={"style": "market"})
    quotes = [q(WE - 150_000, 390, 400), q(WE - 90_000, 470, 480)]
    t = simulate_market(quotes, WE, "up", s)
    assert t.entry_milli == 480  # observed, gap against the buyer
    assert t.entry_taker is True


def test_limit_after_trigger_fills_on_trigger_tick_or_later_or_never():
    cond = [{"type": "time_to_close", "op": "<=", "seconds": 200}]
    s = strat(conditions=cond,
              execution={"style": "limit", "limit_price_cents": 40})
    # ask already at/below the limit on the trigger tick: same-tick fill at limit
    quotes = [q(WE - 190_000, 380, 390)]
    t = simulate_market(quotes, WE, "up", s)
    assert t.entry_milli == 400  # at the limit price, not the observed 390
    assert t.entry_taker is False
    # fills later
    quotes = [q(WE - 190_000, 440, 450), q(WE - 100_000, 390, 400)]
    t = simulate_market(quotes, WE, "up", s)
    assert t.entry_ts_ms == WE - 100_000
    # never fills -> untriggered
    quotes = [q(WE - 190_000, 440, 450), q(WE - 100_000, 460, 470)]
    assert simulate_market(quotes, WE, "up", s) is None


# ------------------------------------------------------------------ exits

def test_stop_loss_fills_at_observed_bid_on_gap():
    # entry at 40c, stop 5c -> level 35c; bid gaps 0.38 -> 0.20:
    # fill at the OBSERVED 20c, not the 35c stop level
    s = strat(execution={"style": "limit", "limit_price_cents": 40},
              exit={"stop_loss_cents": 5})
    quotes = [q(1_000_100, 390, 400), q(1_000_200, 380, 390),
              q(1_000_300, 200, 210)]
    t = simulate_market(quotes, WE, "up", s)
    assert t.exit_kind == "stop_loss"
    assert t.exit_milli == 200
    assert t.pnl_milli == 200 - 400


def test_tp_gap_contrast_fills_at_limit():
    # same gap shape upward: TP fills AT THE LIMIT, never the observed bid
    s = strat(execution={"style": "limit", "limit_price_cents": 40},
              exit={"take_profit_cents": 5})
    quotes = [q(1_000_100, 390, 400), q(1_000_200, 700, 710)]
    t = simulate_market(quotes, WE, "up", s)
    assert t.exit_kind == "take_profit"
    assert t.exit_milli == 450
    assert t.pnl_milli == 50


def test_same_tick_exit_priority_stop_first():
    # stop and time exit due at the same tick: stop wins (documented priority)
    s = strat(execution={"style": "limit", "limit_price_cents": 40},
              exit={"stop_loss_cents": 5, "exit_seconds_before_close": 100})
    quotes = [q(WE - 200_000, 390, 400), q(WE - 100_000, 300, 310)]
    t = simulate_market(quotes, WE, "up", s)
    assert t.exit_kind == "stop_loss"
    # tp and time exit due at the same tick: tp wins over time
    s = strat(execution={"style": "limit", "limit_price_cents": 40},
              exit={"take_profit_cents": 5, "exit_seconds_before_close": 100})
    quotes = [q(WE - 200_000, 390, 400), q(WE - 100_000, 460, 470)]
    t = simulate_market(quotes, WE, "up", s)
    assert t.exit_kind == "take_profit"
    assert t.exit_milli == 450
    # note: stop vs take-profit on one tick is mutually exclusive for a
    # single bid with positive deltas; the coded order still puts stop first


def test_time_exit_at_last_tick_before_threshold():
    s = strat(execution={"style": "limit", "limit_price_cents": 40},
              exit={"exit_seconds_before_close": 60})
    quotes = [
        q(WE - 200_000, 390, 400),  # entry fill
        q(WE - 90_000, 410, 420),
        q(WE - 61_000, 430, 440),   # last tick at/before WE-60s -> sell here
        q(WE - 30_000, 990, 1000),  # never reached
    ]
    t = simulate_market(quotes, WE, "up", s)
    assert t.exit_kind == "time_exit"
    assert t.exit_milli == 430  # observed bid at the scheduled tick


def test_entry_after_scheduled_time_exit_sells_first_tick_after_entry():
    s = strat(conditions=[{"type": "time_to_close", "op": "<=", "seconds": 30}],
              execution={"style": "market"},
              exit={"exit_seconds_before_close": 60})
    quotes = [q(WE - 25_000, 300, 310), q(WE - 10_000, 320, 330)]
    t = simulate_market(quotes, WE, "up", s)
    assert t.entry_ts_ms == WE - 25_000
    assert t.exit_kind == "time_exit"
    assert t.exit_milli == 320


def test_entry_on_final_tick_force_resolves():
    s = strat(execution={"style": "market"},
              exit={"take_profit_cents": 5, "stop_loss_cents": 5,
                    "exit_seconds_before_close": 10})
    quotes = [q(WE - 100, 390, 400)]
    t = simulate_market(quotes, WE, "up", s)
    assert t.exit_kind == "forced_win"
    assert t.pnl_milli == 1000 - 400
    t = simulate_market(quotes, WE, "down", s)
    assert t.exit_kind == "forced_loss"
    assert t.pnl_milli == -400


# ------------------------------------------------------------------- fees

def test_taker_fee_on_taker_legs_only():
    fee = 0.02
    # maker entry + maker tp: zero fees even with taker_fee set
    s = strat(execution={"style": "limit", "limit_price_cents": 40},
              exit={"take_profit_cents": 5})
    quotes = [q(1_000_100, 390, 400), q(1_000_200, 460, 470)]
    t = simulate_market(quotes, WE, "up", s, taker_fee=fee)
    assert t.fees_milli == 0
    assert t.pnl_milli == 50
    # market entry + stop exit: fee on both observed fills
    s = strat(execution={"style": "market"}, exit={"stop_loss_cents": 5})
    quotes = [q(1_000_100, 390, 400), q(1_000_200, 300, 310)]
    t = simulate_market(quotes, WE, "up", s, taker_fee=fee)
    assert t.fees_milli == round(fee * 400) + round(fee * 300)
    assert t.pnl_milli == 300 - 400 - t.fees_milli
    # forced resolution carries no exit fee
    s = strat(execution={"style": "market"})
    quotes = [q(1_000_100, 390, 400)]
    t = simulate_market(quotes, WE, "up", s, taker_fee=fee)
    assert t.fees_milli == round(fee * 400)
    assert t.pnl_milli == 1000 - 400 - t.fees_milli


# ------------------------------------------------------------- look-ahead

def test_no_look_ahead_future_mutation_invariance():
    """Mutating every tick after the decision points must change nothing
    about the decisions already made."""
    s = strat(conditions=[{"type": "price_move", "window_seconds": 10,
                           "op": ">=", "value": 2, "unit": "cents"},
                          {"type": "btc_move", "op": ">=", "value": 1,
                           "unit": "dollars"}],
              execution={"style": "market"},
              exit={"stop_loss_cents": 5})
    quotes = [
        q(1_000_000, 400, 410, 70_000.0),
        q(1_011_000, 420, 430, 70_002.0),  # entry triggers here
        q(1_020_000, 360, 370, 70_010.0),  # stop fills here at 360
        q(1_030_000, 990, 1000, 80_000.0),
        q(1_040_000, 10, 20, 10_000.0),
    ]
    base = simulate_market(quotes, WE, "up", s)
    assert base.entry_ts_ms == 1_011_000 and base.exit_milli == 360
    # poison the future beyond the exit tick
    poisoned = quotes[:3] + [q(1_030_000, -1, -1, None), q(1_040_000, 5, 6, 0.0)]
    t = simulate_market(poisoned, WE, "up", s)
    assert (t.entry_ts_ms, t.entry_milli, t.exit_kind, t.exit_milli) == (
        base.entry_ts_ms, base.entry_milli, base.exit_kind, base.exit_milli)
    # truncating after the exit tick must also change nothing
    t = simulate_market(quotes[:3], WE, "up", s)
    assert (t.entry_ts_ms, t.exit_kind, t.exit_milli) == (
        base.entry_ts_ms, base.exit_kind, base.exit_milli)
    # entry decision alone: mutate everything after the entry tick and the
    # entry must be identical (winner deliberately flipped too: resolution
    # may not affect anything before window end)
    t_up = simulate_market(quotes[:2], WE, "up", s)
    t_dn = simulate_market(quotes[:2], WE, "down", s)
    assert t_up.entry_ts_ms == t_dn.entry_ts_ms == base.entry_ts_ms
    assert t_up.entry_milli == t_dn.entry_milli == base.entry_milli


# ----------------------------------------------------- legacy equivalence

def test_legacy_mapping_equals_v1_engine():
    legacy = legacy_strategy("up", 0.40, 5)
    cases = [
        [q(1, 500, 560), q(2, 500, 550), q(3, 460, 470)],   # untriggered
        [q(1, 390, 400), q(2, 700, 710)],                    # tp on gap
        [q(1, 390, 400)],                                    # forced
        [q(1, 480, 500), q(2, 380, 390), q(3, 449, 455)],    # fill, no tp
    ]
    for quotes in cases:
        for winner in ("up", "down"):
            old = v1.simulate_market([(t, b, a) for t, b, a, _ in quotes],
                                     winner, "up", 400, 450)
            new = simulate_market(quotes, WE, winner, legacy)
            if old is None:
                assert new is None
            else:
                old_kind = {"forced_win": "forced_win",
                            "forced_loss": "forced_loss",
                            "take_profit": "take_profit"}[old.exit_kind]
                assert new.exit_kind == old_kind
                assert new.entry_milli == old.entry_milli
                assert new.exit_milli == old.exit_milli
                assert new.pnl_milli == old.pnl_milli
                assert new.entry_ts_ms == old.entry_ts_ms


# ------------------------------------------------------------- run/stats

def test_one_trade_per_market_and_untriggered_counted():
    s = strat(execution={"style": "limit", "limit_price_cents": 40},
              exit={"take_profit_cents": 5})
    quotes = {"a": [q(i, 380, 390) for i in range(1, 50)],  # many fills: 1 trade
              "b": [q(1, 500, 510)]}                          # untriggered
    markets = [mk("a"), mk("b")]
    res = run_backtest(markets, lambda sl: quotes[sl], s)
    assert len(res.trades) == 1
    assert res.untriggered_markets == 1
    assert summarize(res)["stats"]["num_trades"] == 1


def test_extended_breakdown_sums_to_num_trades():
    s = strat(execution={"style": "market"},
              exit={"take_profit_cents": 5, "stop_loss_cents": 5,
                    "exit_seconds_before_close": 60})
    quotes = {
        "tp":   [q(WE - 200_000, 390, 400), q(WE - 190_000, 460, 470)],
        "stop": [q(WE - 200_000, 390, 400), q(WE - 190_000, 300, 310)],
        "time": [q(WE - 200_000, 390, 400), q(WE - 61_000, 410, 420)],
        "fwin": [q(WE - 100, 390, 400)],
        "flos": [q(WE - 100, 390, 400)],
    }
    markets = [mk("tp"), mk("stop"), mk("time"), mk("fwin", winner="up"),
               mk("flos", winner="down")]
    res = run_backtest(markets, lambda sl: quotes[sl], s)
    out = summarize(res)
    b = out["breakdown"]
    assert b == {"take_profit_exits": 1, "stop_loss_exits": 1, "time_exits": 1,
                 "forced_resolution_wins": 1, "forced_resolution_losses": 1}
    assert sum(b.values()) == out["stats"]["num_trades"] == 5
    fs = out["entry_fill_stats"]
    assert fs["taker_entries"] == 5 and fs["maker_entries"] == 0
    # every market entry paid the ask, mid was 5 milli lower: slippage 0.5c
    assert fs["avg_market_entry_slippage_cents"] == 0.5


# ------------------------------------------------------------- validation

def test_schema_rejects_unknown_and_malformed():
    base = {"side": "up",
            "entry": {"conditions": [], "execution": {"style": "market"}},
            "exit": {}}
    bad = []
    b = copy.deepcopy(base); b["side"] = "sideways"; bad.append(b)
    b = copy.deepcopy(base); b["entry"]["conditions"] = [
        {"type": "moon_phase", "op": "<=", "value_cents": 1}]; bad.append(b)
    b = copy.deepcopy(base); b["entry"]["conditions"] = [
        {"type": "price_level", "op": "==", "value_cents": 30}]; bad.append(b)
    b = copy.deepcopy(base); b["entry"]["conditions"] = [
        {"type": "price_level", "op": "<=", "value_cents": 30,
         "extra": 1}]; bad.append(b)
    b = copy.deepcopy(base); b["entry"]["execution"] = {"style": "stop"}; bad.append(b)
    b = copy.deepcopy(base); b["entry"]["execution"] = {
        "style": "limit", "limit_price_cents": 150}; bad.append(b)
    b = copy.deepcopy(base); b["exit"] = {"take_profit_cents": -5}; bad.append(b)
    b = copy.deepcopy(base); b["exit"] = {"trailing_stop": 5}; bad.append(b)
    b = copy.deepcopy(base); b["leverage"] = 10; bad.append(b)
    for spec in bad:
        with pytest.raises(ValueError) as e:
            validate_strategy(spec)
        assert "vocabulary" in str(e.value).lower()
    # all documented examples must validate
    from pmbt.strategy import VOCABULARY
    for ex in VOCABULARY["examples"]:
        validate_strategy(ex["strategy"])


# ------------------------------------------------- full-data regression

STORE = os.path.join(os.path.dirname(__file__), "..", "data", "store.db")


@pytest.mark.skipif(not os.path.exists(STORE),
                    reason="real data store not present")
def test_regression_anchor_full_universe():
    """The legacy mapping through the new engine must reproduce the
    independently verified v1 result EXACTLY (audited 2026-06)."""
    from pmbt.db import Store
    st = Store(STORE)
    markets = st.markets_for_backtest()
    legacy = legacy_strategy("up", 0.40, 5)
    res = run_backtest(markets, lambda s: st.side_quotes_btc(s, "up"), legacy)
    out = summarize(res)
    assert res.markets_tested == 14361
    assert res.untriggered_markets == 3116
    assert out["stats"]["num_trades"] == 11245
    assert out["stats"]["win_rate"] == 0.7523
    assert out["stats"]["total_pnl"] == -688.8
    assert out["stats"]["expectancy"] == -0.0613
    assert out["breakdown"]["take_profit_exits"] == 8456
    assert out["breakdown"]["forced_resolution_wins"] == 4
    assert out["breakdown"]["forced_resolution_losses"] == 2785
    assert out["breakdown"]["stop_loss_exits"] == 0
    assert out["breakdown"]["time_exits"] == 0
