"""Ingest pipeline: raw Kaggle files -> data/store.db.

Usage:
    python -m pmbt.ingest --raw data_raw --db data/store.db [--download]

Steps:
1. (optional --download) fetch both Kaggle datasets anonymously via kagglehub
   and symlink them into --raw.
2. Load the 100ms outcomes file -> winners for the tradeable universe.
3. Stream the 100ms ticks parquet row group by row group:
   keep in-window rows only, convert prices to integer tenths-of-a-cent,
   drop consecutive rows whose four quote columns did not change, and
   collect per-market aggregates (tick_count, first/last BTC reference).
4. Load the 2s supplementary CSV (metadata only): official
   price_to_beat = btc_current - btc_gap, sanity-checked constant per window,
   plus winners for cross-validation against the outcomes file.
5. Cross-validate winners on overlapping slugs; disagreeing markets are
   logged and excluded from the tradeable universe.
6. Write markets, quotes and meta tables.
"""

import argparse
import json
import os
import re
import sqlite3
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .store import connect_rw

TICKS_FILE = "btc_5m_hf_ticks.parquet"
OUTCOMES_FILE = "btc_5m_market_outcomes.parquet"
SUPP_FILE = "market_data_2sec_weekly5_with_resolutions.csv"

PRIMARY_DATASET = "namz8888/polymarket-btc-5-minute-high-frequency-tick-data"
SUPP_DATASET = "debayan31415/polymarket-5-minutes-btc-up-down-data"

SLUG_RE = re.compile(r"-(\d+)$")


def slug_window(slug: str, duration_minutes: int = 5):
    """Canonical key: numeric suffix is the UTC unix second of window start."""
    m = SLUG_RE.search(slug)
    if not m:
        raise ValueError(f"cannot parse window start from slug: {slug}")
    start_ms = int(m.group(1)) * 1000
    return start_ms, start_ms + duration_minutes * 60_000


def to_milli(arr: np.ndarray) -> np.ndarray:
    """Dollars -> integer tenths of a cent. NaN -> -1 sentinel (never matches)."""
    out = np.full(len(arr), -1, dtype=np.int64)
    ok = ~np.isnan(arr)
    out[ok] = np.rint(arr[ok] * 1000).astype(np.int64)
    return out


def download(raw_dir: str):
    import kagglehub

    os.makedirs(raw_dir, exist_ok=True)
    for ds in (PRIMARY_DATASET, SUPP_DATASET):
        path = kagglehub.dataset_download(ds)
        for name in os.listdir(path):
            dst = os.path.join(raw_dir, name)
            if not os.path.exists(dst):
                os.symlink(os.path.join(path, name), dst)
        print(f"downloaded {ds} -> {path}")


def load_outcomes(raw_dir: str) -> pd.DataFrame:
    out = pd.read_parquet(os.path.join(raw_dir, OUTCOMES_FILE))
    out = out[["event_slug", "window_start_ts", "window_end_ts", "winner_side"]].copy()
    out["winner"] = out["winner_side"].str.lower()
    bad = ~out["winner"].isin(["up", "down"])
    if bad.any():
        print(f"WARN: dropping {bad.sum()} outcome rows with winner not up/down")
        out = out[~bad]
    return out.drop_duplicates("event_slug").set_index("event_slug")


def stream_ticks(raw_dir: str, con: sqlite3.Connection):
    """Stream the 100ms parquet into the quotes table. Returns per-market aggregates."""
    pf = pq.ParquetFile(os.path.join(raw_dir, TICKS_FILE))
    cols = [
        "ts", "event_slug", "binance_price",
        "poly_up_bid", "poly_up_ask", "poly_down_bid", "poly_down_ask",
        "window_start_ts", "window_end_ts", "interval_min",
    ]
    # carry the last stored quote tuple per slug across row-group boundaries
    last_quote = {}
    agg = {}  # slug -> [tick_count, first_btc, last_btc, duration_min]
    total_in, total_kept = 0, 0

    for gi in range(pf.metadata.num_row_groups):
        df = pf.read_row_group(gi, columns=cols).to_pandas()
        ts_ms = np.rint(df["ts"].to_numpy() * 1000).astype(np.int64)
        ws_ms = df["window_start_ts"].to_numpy() * 1000
        we_ms = df["window_end_ts"].to_numpy() * 1000
        in_win = (ts_ms >= ws_ms) & (ts_ms <= we_ms)
        df = df[in_win].copy()
        df["ts_ms"] = ts_ms[in_win]
        total_in += len(df)
        if df.empty:
            continue
        df.sort_values(["event_slug", "ts_ms"], kind="stable", inplace=True)

        slugs = df["event_slug"].to_numpy()
        q = np.stack(
            [
                to_milli(df["poly_up_bid"].to_numpy(dtype=np.float64)),
                to_milli(df["poly_up_ask"].to_numpy(dtype=np.float64)),
                to_milli(df["poly_down_bid"].to_numpy(dtype=np.float64)),
                to_milli(df["poly_down_ask"].to_numpy(dtype=np.float64)),
            ],
            axis=1,
        )
        btc = df["binance_price"].to_numpy(dtype=np.float64)
        tsm = df["ts_ms"].to_numpy()

        # per-market aggregates on the full (pre-dedup) in-window stream
        for slug, g in df.groupby("event_slug", sort=False):
            ref = g["binance_price"].dropna()
            first_btc = float(ref.iloc[0]) if len(ref) else np.nan
            last_btc = float(ref.iloc[-1]) if len(ref) else np.nan
            a = agg.get(slug)
            if a is None:
                agg[slug] = [len(g), first_btc, last_btc, int(g["interval_min"].iloc[0])]
            else:
                a[0] += len(g)
                if np.isnan(a[1]):
                    a[1] = first_btc
                if not np.isnan(last_btc):
                    a[2] = last_btc

        # vectorized dedup: keep a row when the slug changes or any of the
        # four quote columns changed vs the previous row
        slug_change = np.ones(len(df), dtype=bool)
        slug_change[1:] = slugs[1:] != slugs[:-1]
        quote_change = np.ones(len(df), dtype=bool)
        quote_change[1:] = (q[1:] != q[:-1]).any(axis=1)
        keep = slug_change | quote_change
        # cross-boundary: drop a slug's first row in this group if it equals
        # the last quote stored for that slug in a previous group
        for i in np.flatnonzero(slug_change):
            if last_quote.get(slugs[i]) == tuple(q[i]):
                keep[i] = False
        # remember the final quote per slug seen in this group
        block_ends = np.append(np.flatnonzero(slug_change)[1:] - 1, len(df) - 1)
        for i in block_ends:
            last_quote[slugs[i]] = tuple(q[i])

        kept_idx = np.flatnonzero(keep)
        total_kept += len(kept_idx)
        rows = [
            (
                slugs[i], int(tsm[i]),
                int(q[i, 0]), int(q[i, 1]), int(q[i, 2]), int(q[i, 3]),
                None if np.isnan(btc[i]) else float(btc[i]),
            )
            for i in kept_idx
        ]
        con.executemany(
            "INSERT INTO quotes (slug, ts_ms, up_bid, up_ask, down_bid, down_ask, btc_ref)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        if gi % 25 == 0:
            con.commit()
            print(f"  row group {gi + 1}/{pf.metadata.num_row_groups}: "
                  f"{total_in:,} in-window rows, {total_kept:,} kept after dedup")
    con.commit()
    print(f"ticks done: {total_in:,} in-window rows -> {total_kept:,} quote changes stored")
    return agg


def load_supplementary(raw_dir: str):
    """2s dataset -> per-market metadata. Never traded on."""
    path = os.path.join(raw_dir, SUPP_FILE)
    if not os.path.exists(path):
        print("supplementary 2s file not found, skipping")
        return {}, []
    supp = pd.read_csv(path)
    log = []
    out = {}
    for slug, g in supp.groupby("slug"):
        ptb_series = (g["btc_current"] - g["btc_gap"]).round(3)
        ptb = None
        # official price_to_beat must be constant within the window
        if ptb_series.notna().any():
            vals = ptb_series.dropna()
            if vals.max() - vals.min() <= 0.011:
                ptb = float(vals.iloc[0])
            else:
                log.append(
                    f"{slug}: price_to_beat not constant "
                    f"(min={vals.min()}, max={vals.max()}), nulled"
                )
        winner = None
        if "winner" in g and g["winner"].notna().any():
            w = str(g["winner"].dropna().iloc[-1]).lower()
            winner = w if w in ("up", "down") else None
        out[slug] = {"price_to_beat": ptb, "winner": winner}
    return out, log


def run(raw_dir: str, db_path: str):
    if os.path.exists(db_path):
        os.remove(db_path)
    con = connect_rw(db_path)

    print("loading outcomes ...")
    outcomes = load_outcomes(raw_dir)
    print(f"  {len(outcomes)} resolved markets in primary outcomes file")

    print("streaming 100ms ticks ...")
    agg = stream_ticks(raw_dir, con)

    print("loading supplementary 2s dataset ...")
    supp, supp_log = load_supplementary(raw_dir)
    print(f"  {len(supp)} markets in 2s dataset, {len(supp_log)} price_to_beat violations")

    # cross-validate winners on overlapping slugs
    overlap = [s for s in supp if s in outcomes.index]
    disagreements = []
    for s in overlap:
        w2 = supp[s]["winner"]
        w1 = outcomes.loc[s, "winner"]
        if w2 is not None and w2 != w1:
            disagreements.append(f"{s}: primary={w1} supplementary={w2}")
    print(f"  overlap with primary outcomes: {len(overlap)} markets, "
          f"{len(disagreements)} winner disagreements")

    excluded = {d.split(":")[0] for d in disagreements}

    market_rows = []
    # tradeable universe: in primary outcomes AND has in-window quotes,
    # minus cross-validation disagreements
    for slug, row in outcomes.iterrows():
        ws_ms, we_ms = slug_window(slug)
        assert ws_ms == row["window_start_ts"] * 1000, f"slug/window mismatch: {slug}"
        a = agg.get(slug)
        tick_count = a[0] if a else 0
        tradeable = 1 if (tick_count > 0 and slug not in excluded) else 0
        sup = supp.get(slug)
        if sup and sup["price_to_beat"] is not None:
            ptb, src = sup["price_to_beat"], "official"
        elif a and not np.isnan(a[1]):
            # display-only fallback: exchange reference at first in-window tick
            ptb, src = float(a[1]), "approx_from_reference"
        else:
            ptb, src = None, None
        market_rows.append((
            slug, ws_ms, we_ms, a[3] if a else 5, row["winner"], ptb, src,
            "primary_outcomes", tradeable, tick_count,
            None if not a or np.isnan(a[1]) else float(a[1]),
            None if not a or np.isnan(a[2]) else float(a[2]),
        ))

    # supplementary-only markets: metadata rows, never tradeable
    for slug, sup in supp.items():
        if slug in outcomes.index:
            continue
        ws_ms, we_ms = slug_window(slug)
        market_rows.append((
            slug, ws_ms, we_ms, 5, sup["winner"], sup["price_to_beat"],
            "official" if sup["price_to_beat"] is not None else None,
            "supplementary_2s", 0, 0, None, None,
        ))

    con.executemany(
        "INSERT INTO markets (slug, window_start_ms, window_end_ms, duration_minutes,"
        " winner, price_to_beat, price_to_beat_source, resolution_source, tradeable,"
        " tick_count, btc_ref_first, btc_ref_last) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        market_rows,
    )

    validation = {
        "supp_price_to_beat_violations": supp_log,
        "winner_disagreements": disagreements,
        "overlap_markets": len(overlap),
    }
    con.execute(
        "INSERT INTO meta (key, value) VALUES ('validation', ?)",
        (json.dumps(validation),),
    )
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"store written: {db_path} "
          f"({os.path.getsize(db_path) / 1e6:.1f} MB, {len(market_rows)} markets)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data_raw")
    ap.add_argument("--db", default="data/store.db")
    ap.add_argument("--download", action="store_true")
    args = ap.parse_args()
    if args.download:
        download(args.raw)
    run(args.raw, args.db)
    sys.exit(0)
