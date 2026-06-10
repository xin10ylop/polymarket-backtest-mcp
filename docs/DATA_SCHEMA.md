# Data schema

Raw inputs are two Kaggle datasets. The ingest pipeline turns them into a
single SQLite file (`data/store.db`) that the server opens read-only.
Run `python scripts/discover_schema.py` to reproduce the raw-schema dump;
the captured output lives in `docs/schema_discovery_output.txt`.

## Raw dataset 1 (primary): Polymarket BTC 5-Minute 100ms Market Data

Kaggle: `namz8888/polymarket-btc-5-minute-high-frequency-tick-data`

### btc_5m_hf_ticks.parquet (42,708,586 rows)

| column | type | meaning |
|---|---|---|
| ts | double | unix seconds (fractional) of the snapshot |
| seq | int64 | snapshot sequence number |
| datetime | timestamp[ns, UTC] | same instant as ts |
| event_slug | string | market key, e.g. `btc-updown-5m-1772733900` |
| expires | string | ISO window end |
| binance_price / coinbase_price / kraken_price | double | exchange BTC reference feeds, display only |
| poly_up_bid / poly_up_ask | double | best bid/ask of the Up token, dollars |
| poly_down_bid / poly_down_ask | double | best bid/ask of the Down token, dollars |
| interval_min | int32 | market duration in minutes (5) |
| window_start_ts / window_end_ts | int64 | unix seconds of the market window |
| offset_s, up_mid, down_mid, up_spread, down_spread | double | derived, not used by ingest |

Date range: 2026-03-05 18:05 UTC to 2026-04-25 02:50 UTC.

### btc_5m_market_outcomes.parquet (14,361 rows)

| column | type | meaning |
|---|---|---|
| event_slug | string | market key |
| window_start_ts / window_end_ts | int64 | unix seconds |
| winner_side | string | `UP` or `DOWN`, the resolved outcome (ground truth) |
| target | int8 | 1 if UP won |

This file is the only source of winners for tradeable markets. Winners are
never derived from the exchange reference prices because Polymarket resolves
on the Chainlink BTC/USD stream, and the prints differ enough to flip close
calls. The official rule (end price >= start price means Up, ties go Up) is
already encoded in `winner_side`.

## Raw dataset 2 (supplementary, metadata only): Polymarket 5 minutes BTC UP Down data

Kaggle: `debayan31415/polymarket-5-minutes-btc-up-down-data`

### market_data_2sec_weekly5_with_resolutions.csv (113,245 rows, 1,191 markets)

| column | type | meaning |
|---|---|---|
| slug | string | market key |
| start_time | int64 | unix seconds of window start |
| elapsed | int64 | seconds since window start |
| ask_YES / bid_YES / ask_NO / bid_NO | float | 2s book snapshots (never traded on) |
| btc_strike | float | official strike |
| btc_current / btc_gap | float | current Chainlink price and gap to strike |
| timestamp_log | int64 | unix seconds of the snapshot |
| resolved | bool | resolution flag |
| winner | string | `Up` / `Down` |

Date range: 2026-02-23 11:50 UTC to 2026-03-05 03:35 UTC.

Used only for:

1. official `price_to_beat = btc_current - btc_gap` (sanity check: must be
   constant within each window; violators are logged and nulled; the dataset
   has 0 violators),
2. cross-validating winners against the outcomes file on overlapping slugs.

**Overlap note:** the two datasets share zero slugs (the 2s data ends
2026-03-05 03:35, the 100ms data starts 2026-03-05 18:05). So in the current
store, no tradeable market has an official price_to_beat, and cross-validation
had nothing to compare. The pipeline handles zero, partial or full overlap;
markets that disagree on the winner would be excluded from the tradeable
universe. Markets that exist only in the 2s dataset are kept as metadata rows
but are never backtestable (2s sampling misses price touches).

## Canonical market key

The numeric slug suffix is the UTC unix second of the window start (always
divisible by 300 for 5m markets, verified at ingest):

```
window_start_ms = suffix * 1000
window_end_ms   = window_start_ms + duration_minutes * 60_000
```

This is the cross-dataset join key. All lookups are keyed strictly by slug;
price data is never mixed across markets or durations.

## Processed store (SQLite, data/store.db)

### markets (15,552 rows: 14,361 primary + 1,191 supplementary-only)

| column | type | notes |
|---|---|---|
| slug | TEXT PK | market key |
| window_start_ms / window_end_ms | INTEGER | UTC unix ms |
| duration_minutes | INTEGER | 5 for now, engine is duration agnostic |
| winner | TEXT | `up` / `down` / NULL |
| price_to_beat | REAL NULL | display only, never used in PnL |
| price_to_beat_source | TEXT | `official` / `approx_from_reference` / NULL |
| resolution_source | TEXT | `primary_outcomes` / `supplementary_2s` |
| tradeable | INTEGER | 1 only if in primary outcomes AND has in-window quotes AND passed cross-validation |
| tick_count | INTEGER | raw in-window 100ms ticks |
| btc_ref_first / btc_ref_last | REAL | first/last reference price in window |

`approx_from_reference` is the Binance reference price at the market's first
in-window tick. It is an exchange feed, not the Chainlink strike, and is
display only.

### quotes (6,923,207 rows)

In-window 100ms snapshots, deduplicated to rows where any of the four quote
columns changed. Prices are INTEGER tenths of a cent (0.47 is stored as 470)
so fill checks never compare raw floats; -1 marks a missing quote. Quotes
outside the market window are dropped at ingest.

| column | type |
|---|---|
| slug | TEXT |
| ts_ms | INTEGER |
| up_bid / up_ask / down_bid / down_ask | INTEGER (tenths of a cent) |
| btc_ref | REAL NULL |

### meta

Key/value JSON blobs. `validation` holds the cross-validation log
(price_to_beat violations, winner disagreements, overlap count).
