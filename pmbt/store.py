"""SQLite store for processed market data.

One file, two tables, opened read-only at runtime.

markets:  one row per market, keyed by slug. winner comes from the 100ms
          outcomes file for tradeable markets (resolution_source='primary_outcomes')
          or from the 2s dataset for supplementary-only markets
          (resolution_source='supplementary_2s', tradeable=0).
quotes:   in-window 100ms best bid/ask snapshots, deduplicated to quote
          changes. Prices are stored as INTEGER tenths-of-a-cent
          (0.47 -> 470) so fill checks never compare raw floats.

All timestamps are UTC unix milliseconds.
"""

import os
import sqlite3

DEFAULT_DB_PATH = os.environ.get(
    "PMBT_DB", os.path.join(os.path.dirname(__file__), "..", "data", "store.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    slug TEXT PRIMARY KEY,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    duration_minutes INTEGER NOT NULL,
    winner TEXT,                      -- 'up' / 'down' / NULL
    price_to_beat REAL,               -- nullable, display only
    price_to_beat_source TEXT,        -- 'official' / 'approx_from_reference' / NULL
    resolution_source TEXT,           -- 'primary_outcomes' / 'supplementary_2s'
    tradeable INTEGER NOT NULL,       -- 1 only if in primary outcomes AND has quotes
    tick_count INTEGER NOT NULL DEFAULT 0,
    btc_ref_first REAL,
    btc_ref_last REAL
);
CREATE INDEX IF NOT EXISTS idx_markets_window ON markets (window_start_ms);

CREATE TABLE IF NOT EXISTS quotes (
    slug TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    up_bid INTEGER, up_ask INTEGER,   -- tenths of a cent (0.47 -> 470)
    down_bid INTEGER, down_ask INTEGER,
    btc_ref REAL
);
CREATE INDEX IF NOT EXISTS idx_quotes_slug_ts ON quotes (slug, ts_ms);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect_rw(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def connect_ro(path: str = None) -> sqlite3.Connection:
    path = path or DEFAULT_DB_PATH
    # read-only and immutable, so sharing across worker threads is safe
    con = sqlite3.connect(
        f"file:{os.path.abspath(path)}?mode=ro", uri=True, check_same_thread=False
    )
    con.row_factory = sqlite3.Row
    return con
