"""Read-only query layer between the SQLite store and the MCP tools."""

import json
from datetime import datetime, timezone

from .store import connect_ro

SIDE_COLS = {"up": ("up_bid", "up_ask"), "down": ("down_bid", "down_ask")}


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def parse_date(s: str | None, end: bool = False):
    """'YYYY-MM-DD' (UTC) -> unix ms at start (or exclusive end) of day."""
    if not s:
        return None
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return ms + 86_400_000 if end else ms


class Store:
    def __init__(self, path: str = None):
        self.con = connect_ro(path)

    def coverage(self) -> dict:
        con = self.con
        total, tradeable, official = con.execute(
            "SELECT COUNT(*), SUM(tradeable),"
            " SUM(CASE WHEN price_to_beat_source='official' THEN 1 ELSE 0 END)"
            " FROM markets"
        ).fetchone()
        first, last = con.execute(
            "SELECT MIN(window_start_ms), MAX(window_start_ms) FROM markets WHERE tradeable=1"
        ).fetchone()
        rows = con.execute(
            "SELECT date(window_start_ms/1000,'unixepoch') d, COUNT(*) c"
            " FROM markets WHERE tradeable=1 GROUP BY d ORDER BY d"
        ).fetchall()
        per_day = {r["d"]: r["c"] for r in rows}
        # a full UTC day holds 1440/duration markets per tradeable duration
        # (288 for 5m); report missing and partial days against that
        full_day = sum(
            1440 // r[0]
            for r in con.execute(
                "SELECT DISTINCT duration_minutes FROM markets WHERE tradeable=1"
            )
        )
        gaps = []
        if rows:
            from datetime import date, timedelta
            d0 = date.fromisoformat(rows[0]["d"])
            d1 = date.fromisoformat(rows[-1]["d"])
            d = d0
            while d <= d1:
                c = per_day.get(d.isoformat(), 0)
                if c == 0:
                    gaps.append({"date": d.isoformat(), "tradeable_markets": 0,
                                 "note": "no data"})
                elif c < full_day:
                    gaps.append({"date": d.isoformat(), "tradeable_markets": c,
                                 "note": "partial day"})
                d += timedelta(days=1)
        validation = con.execute(
            "SELECT value FROM meta WHERE key='validation'"
        ).fetchone()
        supp_n, supp_first, supp_last = con.execute(
            "SELECT COUNT(*), MIN(window_start_ms), MAX(window_start_ms)"
            " FROM markets WHERE tradeable=0"
        ).fetchone()
        return {
            "first_market_utc": iso(first) if first else None,
            "last_market_utc": iso(last) if last else None,
            "total_days_covered": len(per_day),
            "range_scope": (
                "first/last/days refer to TRADEABLE (backtestable) markets only; "
                "supplementary-only markets may fall outside this range"
            ),
            "total_markets": total,
            "tradeable_markets": tradeable or 0,
            "supplementary_only_markets": supp_n,
            "supplementary_only_first_utc": iso(supp_first) if supp_first else None,
            "supplementary_only_last_utc": iso(supp_last) if supp_last else None,
            "markets_with_official_price_to_beat": official or 0,
            "official_price_to_beat_scope": (
                "counted over ALL markets; with the current zero-overlap datasets "
                "every official price_to_beat belongs to a non-tradeable "
                "supplementary-only market"
            ),
            "date_gaps": gaps,
            "cross_validation": json.loads(validation["value"]) if validation else None,
        }

    def list_markets(self, duration_minutes=5, resolved=None, start_ms=None,
                     end_ms=None, limit=100, offset=0):
        q = "SELECT * FROM markets WHERE duration_minutes=?"
        args = [duration_minutes]
        if resolved is True:
            q += " AND winner IS NOT NULL"
        elif resolved is False:
            q += " AND winner IS NULL"
        if start_ms is not None:
            q += " AND window_start_ms >= ?"
            args.append(start_ms)
        if end_ms is not None:
            q += " AND window_start_ms < ?"
            args.append(end_ms)
        q += " ORDER BY window_start_ms LIMIT ? OFFSET ?"
        # floor as well as cap: a negative limit would become SQLite's
        # LIMIT -1 (unlimited) and bypass the row cap
        args += [max(1, min(int(limit), 1000)), max(0, int(offset))]
        return [self._market_dict(r) for r in self.con.execute(q, args)]

    def get_market(self, slug: str):
        r = self.con.execute("SELECT * FROM markets WHERE slug=?", (slug,)).fetchone()
        return dict(r) if r else None

    @staticmethod
    def _market_dict(r) -> dict:
        return {
            "slug": r["slug"],
            "window_start_utc": iso(r["window_start_ms"]),
            "window_end_utc": iso(r["window_end_ms"]),
            "window_start_ms": r["window_start_ms"],
            "window_end_ms": r["window_end_ms"],
            "duration_minutes": r["duration_minutes"],
            "winner": r["winner"],
            "price_to_beat": r["price_to_beat"],
            "price_to_beat_source": r["price_to_beat_source"],
            "tick_count": r["tick_count"],
            "tradeable": bool(r["tradeable"]),
        }

    def quotes(self, slug: str):
        """All stored quote changes for one market, time ordered."""
        return self.con.execute(
            "SELECT ts_ms, up_bid, up_ask, down_bid, down_ask, btc_ref"
            " FROM quotes WHERE slug=? ORDER BY ts_ms", (slug,)
        ).fetchall()

    def side_quotes(self, slug: str, side: str):
        """(ts_ms, bid_milli, ask_milli) tuples for one side of one market."""
        bid, ask = SIDE_COLS[side]
        return self.con.execute(
            f"SELECT ts_ms, {bid}, {ask} FROM quotes WHERE slug=? ORDER BY ts_ms",
            (slug,),
        ).fetchall()

    def side_quotes_btc(self, slug: str, side: str):
        """(ts_ms, bid_milli, ask_milli, btc_ref) tuples for the strategy
        engine. btc_ref is the display-grade reference feed: usable as a
        signal input only, never for resolution."""
        bid, ask = SIDE_COLS[side]
        return self.con.execute(
            f"SELECT ts_ms, {bid}, {ask}, btc_ref FROM quotes"
            " WHERE slug=? ORDER BY ts_ms",
            (slug,),
        ).fetchall()

    def markets_for_backtest(self, duration_minutes=5, start_ms=None, end_ms=None,
                             max_markets=None):
        q = ("SELECT slug, window_start_ms, window_end_ms, winner, tradeable"
             " FROM markets"
             " WHERE duration_minutes=? AND tradeable=1 AND winner IS NOT NULL")
        args = [duration_minutes]
        if start_ms is not None:
            q += " AND window_start_ms >= ?"
            args.append(start_ms)
        if end_ms is not None:
            q += " AND window_start_ms < ?"
            args.append(end_ms)
        q += " ORDER BY window_start_ms"
        if max_markets:
            q += " LIMIT ?"
            args.append(int(max_markets))
        return [dict(r) for r in self.con.execute(q, args)]
