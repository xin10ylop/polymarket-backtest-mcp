"""Schema discovery for the raw Kaggle datasets.

Prints column names, dtypes, sample rows, timestamp formats, date ranges,
and one full market's worth of rows for each dataset. Run this before
touching the ingest pipeline so nothing about the files is assumed.
"""

import sys

import pandas as pd
import pyarrow.parquet as pq

RAW = "data_raw"
TICKS = f"{RAW}/btc_5m_hf_ticks.parquet"
OUTCOMES = f"{RAW}/btc_5m_market_outcomes.parquet"
SUPP = f"{RAW}/market_data_2sec_weekly5_with_resolutions.csv"

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 250)


def header(title):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def ts_guess(series, name):
    if series.dtype == bool:
        print(f"  {name}: bool, value_counts={series.value_counts().to_dict()}")
        return
    s = pd.to_numeric(series.dropna(), errors="coerce").dropna()
    if s.empty:
        print(f"  {name}: not numeric, sample={series.dropna().head(3).tolist()}")
        return
    v = s.iloc[0]
    if v > 1e17:
        unit = "ns"
    elif v > 1e14:
        unit = "us"
    elif v > 1e11:
        unit = "ms"
    else:
        unit = "s"
    lo = pd.to_datetime(s.min(), unit=unit, utc=True)
    hi = pd.to_datetime(s.max(), unit=unit, utc=True)
    print(f"  {name}: numeric, inferred unit={unit}, range {lo} .. {hi}")


header("1. PRIMARY ticks file: btc_5m_hf_ticks.parquet")
pf = pq.ParquetFile(TICKS)
print("rows:", pf.metadata.num_rows, " row_groups:", pf.metadata.num_row_groups)
print("\nschema:")
print(pf.schema_arrow)

head = pf.read_row_group(0).to_pandas().head(8)
print("\nfirst 8 rows:")
print(head)

header("2. PRIMARY outcomes file: btc_5m_market_outcomes.parquet")
out = pd.read_parquet(OUTCOMES)
print("rows:", len(out))
print("\ndtypes:")
print(out.dtypes)
print("\nfirst 8 rows:")
print(out.head(8))
print("\nlast 3 rows:")
print(out.tail(3))
for c in out.columns:
    if out[c].dtype == object:
        print(f"  {c}: n_unique={out[c].nunique()}, sample={out[c].dropna().unique()[:5]}")
    elif "int" in str(out[c].dtype) or "float" in str(out[c].dtype):
        ts_guess(out[c], c)

header("3. SUPPLEMENTARY 2s file: market_data_2sec_weekly5_with_resolutions.csv")
supp = pd.read_csv(SUPP)
print("rows:", len(supp))
print("\ndtypes:")
print(supp.dtypes)
print("\nfirst 8 rows:")
print(supp.head(8))
for c in supp.columns:
    if supp[c].dtype == object:
        print(f"  {c}: n_unique={supp[c].nunique()}, sample={supp[c].dropna().unique()[:3]}")
    else:
        ts_guess(supp[c], c)

header("4. Date ranges and overlap")
import re


def slug_start(slug):
    m = re.search(r"(\d+)$", str(slug))
    return int(m.group(1)) if m else None

# outcomes slugs
slug_col_out = next(c for c in out.columns if "slug" in c.lower())
out_starts = out[slug_col_out].map(slug_start).dropna()
print(f"outcomes slug col: {slug_col_out}")
print("  all suffixes divisible by 300:", bool((out_starts % 300 == 0).all()))
print("  outcomes window starts:",
      pd.to_datetime(out_starts.min(), unit="s", utc=True), "..",
      pd.to_datetime(out_starts.max(), unit="s", utc=True),
      f"({out_starts.nunique()} unique markets)")

slug_col_supp = next(c for c in supp.columns if "slug" in c.lower())
supp_starts = supp[slug_col_supp].map(slug_start).dropna()
print(f"supp slug col: {slug_col_supp}")
print("  all suffixes divisible by 300:", bool((supp_starts % 300 == 0).all()))
print("  supp window starts:",
      pd.to_datetime(supp_starts.min(), unit="s", utc=True), "..",
      pd.to_datetime(supp_starts.max(), unit="s", utc=True),
      f"({supp_starts.nunique()} unique markets)")

overlap = set(out[slug_col_out]) & set(supp[slug_col_supp])
print(f"slug overlap between outcomes and 2s dataset: {len(overlap)} markets")

header("5. One full market from the ticks file")
# grab the slug of the first row group's first row
slug_col_ticks = next(c for c in head.columns if "slug" in c.lower())
target = head[slug_col_ticks].iloc[0]
print("target market:", target)
rows = []
for i in range(pf.metadata.num_row_groups):
    t = pf.read_row_group(i).to_pandas()
    sel = t[t[slug_col_ticks] == target]
    if len(sel):
        rows.append(sel)
    elif rows:
        break
mkt = pd.concat(rows)
print("rows for this market:", len(mkt))
print(mkt.head(10))
print("...")
print(mkt.tail(5))
tcol = next((c for c in mkt.columns if "time" in c.lower() or c.lower().endswith("ts") or "ts_" in c.lower()), None)
print("candidate time columns:", [c for c in mkt.columns if "time" in c.lower() or "ts" in c.lower()])
sys.stdout.flush()
