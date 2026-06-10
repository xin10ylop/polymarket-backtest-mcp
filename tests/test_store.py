"""Store-level tests: universe filtering, date ranges, no leakage."""

import pytest

from pmbt import store as store_mod
from pmbt.db import Store, parse_date
from pmbt.engine import run_backtest, summarize

DAY1 = 1773100800000  # 2026-03-10T00:00:00Z
DAY2 = DAY1 + 86_400_000


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "store.db")
    con = store_mod.connect_rw(path)
    markets = [
        # slug, ws, we, dur, winner, ptb, ptb_src, res_src, tradeable, ticks
        ("btc-updown-5m-1773100800", DAY1, DAY1 + 300_000, 5, "up",
         68413.4, "approx_from_reference", "primary_outcomes", 1, 100, 68413.4, 68460.0),
        ("btc-updown-5m-1773187200", DAY2, DAY2 + 300_000, 5, "down",
         None, None, "primary_outcomes", 1, 100, None, None),
        # 2s-only market: has a winner and official price_to_beat, NOT tradeable
        ("btc-updown-5m-1771847400", 1771847400000, 1771847700000, 5, "up",
         66180.25, "official", "supplementary_2s", 0, 0, None, None),
    ]
    con.executemany(
        "INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", markets
    )
    quotes = [
        # day1 market: up ask touches 400, later up bid touches 450
        ("btc-updown-5m-1773100800", DAY1 + 1000, 390, 400, 590, 600, 68413.4),
        ("btc-updown-5m-1773100800", DAY1 + 2000, 460, 470, 520, 530, 68420.0),
        # day2 market: up never fills (ask stays high), down side would fill
        ("btc-updown-5m-1773187200", DAY2 + 1000, 590, 600, 390, 400, None),
    ]
    con.executemany("INSERT INTO quotes VALUES (?,?,?,?,?,?,?)", quotes)
    con.execute("INSERT INTO meta VALUES ('validation', '{}')")
    con.commit()
    con.close()
    return Store(path)


def test_2s_only_market_not_in_backtest_universe(db):
    universe = db.markets_for_backtest()
    slugs = {m["slug"] for m in universe}
    assert "btc-updown-5m-1771847400" not in slugs
    assert len(universe) == 2


def test_date_filter_matches_coverage(db):
    universe = db.markets_for_backtest(
        start_ms=parse_date("2026-03-10"), end_ms=parse_date("2026-03-10", end=True)
    )
    assert [m["slug"] for m in universe] == ["btc-updown-5m-1773100800"]
    res = run_backtest(universe, lambda s: db.side_quotes(s, "up"), "up", 0.40, 5)
    assert res.markets_in_range == 1
    assert res.markets_tested == 1
    # coverage fields derive from the same filtered universe
    starts = [m["window_start_ms"] for m in universe]
    assert min(starts) == max(starts) == DAY1


def test_side_quotes_do_not_mix_sides_or_markets(db):
    # 'up' on day2 market: up ask is 600, never <= 400, even though the
    # DOWN side and the OTHER market both have fill-triggering quotes
    universe = db.markets_for_backtest(start_ms=DAY2)
    res = run_backtest(universe, lambda s: db.side_quotes(s, "up"), "up", 0.40, 5)
    assert res.untriggered_markets == 1
    assert len(res.trades) == 0
    # the down side on the same market does fill
    res = run_backtest(universe, lambda s: db.side_quotes(s, "down"), "down", 0.40, 5)
    assert len(res.trades) == 1


def test_backtest_universe_carries_no_price_to_beat(db):
    """Approximate or official price_to_beat physically cannot reach PnL code:
    the backtest universe rows only contain slug, window, winner, tradeable."""
    for m in db.markets_for_backtest():
        assert set(m.keys()) == {"slug", "window_start_ms", "winner", "tradeable"}


def test_full_pipeline_on_fixture(db):
    universe = db.markets_for_backtest()
    res = run_backtest(universe, lambda s: db.side_quotes(s, "up"), "up", 0.40, 5)
    s = summarize(res)
    # day1: fills at 400, bid hits 460 >= 450 target -> TP exit at 450 (+5c)
    assert s["breakdown"]["take_profit_exits"] == 1
    assert s["stats"]["num_trades"] == 1
    assert res.untriggered_markets == 1
    assert s["stats"]["total_pnl"] == 0.05
