"""AUDIT V3: independent recompute of the example backtest from RAW parquet.

Bypasses the SQLite store and the engine entirely. Implements the spec
directly on the un-deduplicated 100ms ticks:
  - side=up, entry=0.40, TP=5c
  - entry: first in-window tick with poly_up_ask <= 0.40 (integer milli compare)
  - TP: first tick STRICTLY AFTER the entry tick with poly_up_bid >= 0.45
  - else forced resolution by outcomes winner_side
"""

import json

import numpy as np
import pyarrow.parquet as pq
import pandas as pd

ENTRY = 400
TP = 450

pf = pq.ParquetFile("data_raw/btc_5m_hf_ticks.parquet")
cols = ["ts", "event_slug", "poly_up_bid", "poly_up_ask",
        "window_start_ts", "window_end_ts"]

# state per market: 0=no entry yet, 1=entered (waiting for TP), 2=TP filled
state = {}
entry_ts = {}

for gi in range(pf.metadata.num_row_groups):
    df = pf.read_row_group(gi, columns=cols).to_pandas()
    ts_ms = np.rint(df["ts"].to_numpy() * 1000).astype(np.int64)
    in_win = (ts_ms >= df["window_start_ts"].to_numpy() * 1000) & (
        ts_ms <= df["window_end_ts"].to_numpy() * 1000
    )
    df = df[in_win]
    ts_ms = ts_ms[in_win]
    if df.empty:
        continue
    order = np.lexsort((ts_ms, df["event_slug"].to_numpy()))
    slugs = df["event_slug"].to_numpy()[order]
    tsm = ts_ms[order]
    bid = df["poly_up_bid"].to_numpy(dtype=np.float64)[order]
    ask = df["poly_up_ask"].to_numpy(dtype=np.float64)[order]
    bid_m = np.where(np.isnan(bid), -1, np.rint(np.nan_to_num(bid) * 1000)).astype(np.int64)
    ask_m = np.where(np.isnan(ask), -1, np.rint(np.nan_to_num(ask) * 1000)).astype(np.int64)

    starts = np.flatnonzero(np.concatenate(([True], slugs[1:] != slugs[:-1])))
    ends = np.append(starts[1:], len(slugs))
    for s0, s1 in zip(starts, ends):
        slug = slugs[s0]
        st = state.get(slug, 0)
        if st == 2:
            continue
        lo = s0
        if st == 0:
            hits = np.flatnonzero((ask_m[s0:s1] >= 0) & (ask_m[s0:s1] <= ENTRY))
            if len(hits) == 0:
                continue
            ei = s0 + hits[0]
            state[slug] = 1
            entry_ts[slug] = int(tsm[ei])
            lo = ei + 1  # TP only on ticks strictly after the entry tick
        hits = np.flatnonzero(bid_m[lo:s1] >= TP)
        if len(hits):
            state[slug] = 2

outcomes = pd.read_parquet("data_raw/btc_5m_market_outcomes.parquet")
winner = dict(zip(outcomes["event_slug"], outcomes["winner_side"].str.lower()))

# tradeable universe per spec: outcome row AND >=1 in-window tick
universe = [s for s in winner if s in state or s not in state]
with_quotes = set(state) | set(entry_ts)
all_tick_slugs = set(state.keys())

n_untrig = n_tp = n_fwin = n_floss = 0
pnl_milli = 0
for slug, w in winner.items():
    st = state.get(slug)
    if st is None:
        continue  # no in-window ticks -> not tradeable (counted separately)
    if st == 0:
        n_untrig += 1
    elif st == 2:
        n_tp += 1
        pnl_milli += TP - ENTRY
    elif w == "up":
        n_fwin += 1
        pnl_milli += 1000 - ENTRY
    else:
        n_floss += 1
        pnl_milli += -ENTRY

no_tick_markets = [s for s in winner if s not in state]
n_trades = n_tp + n_fwin + n_floss
print(json.dumps({
    "markets_with_outcome": len(winner),
    "markets_with_outcome_but_no_inwindow_ticks": len(no_tick_markets),
    "markets_tested": len(winner) - len(no_tick_markets),
    "untriggered": n_untrig,
    "num_trades": n_trades,
    "tp_exits": n_tp,
    "forced_wins": n_fwin,
    "forced_losses": n_floss,
    "win_rate": round((n_tp + n_fwin) / n_trades, 4),
    "total_pnl_usd": pnl_milli / 1000,
    "expectancy_usd": round(pnl_milli / n_trades / 1000, 4),
}, indent=1))
