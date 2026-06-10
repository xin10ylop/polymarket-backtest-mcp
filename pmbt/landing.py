"""Static landing page served at /."""

EXAMPLE_PROMPT = (
    "Backtest buying Up at 40¢ with a 5¢ take-profit across all markets "
    "and show me the win rate, the skew breakdown, and the date range tested"
)


def landing_html(connector_url: str, coverage: dict) -> str:
    cov_line = "coverage loading..."
    if coverage.get("first_market_utc"):
        cov_line = (
            f"{coverage['first_market_utc'][:10]} to "
            f"{coverage['last_market_utc'][:10]} UTC &middot; "
            f"{coverage.get('tradeable_markets', 0):,} tradeable markets &middot; "
            f"{coverage.get('total_days_covered', 0)} days"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket BTC Backtester - MCP Server</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
    background: #0d1117; color: #e6edf3; line-height: 1.6;
    max-width: 760px; margin: 0 auto; padding: 48px 24px 96px;
  }}
  h1 {{ font-size: 1.9rem; margin-bottom: 8px; }}
  h2 {{ font-size: 1.15rem; margin: 40px 0 12px; color: #f0b429; }}
  p.tagline {{ color: #8b949e; font-size: 1.05rem; }}
  .url-box {{
    display: flex; align-items: center; gap: 12px; margin: 24px 0 6px;
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 14px 16px;
  }}
  .url-box code {{
    flex: 1; font-size: 0.95rem; color: #7ee787; overflow-wrap: anywhere;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }}
  button {{
    background: #238636; color: #fff; border: 0; border-radius: 8px;
    padding: 8px 16px; font-size: 0.9rem; cursor: pointer; white-space: nowrap;
  }}
  button:hover {{ background: #2ea043; }}
  .coverage {{ color: #8b949e; font-size: 0.9rem; }}
  ol, ul {{ padding-left: 24px; }}
  li {{ margin: 6px 0; }}
  .prompt {{
    background: #161b22; border: 1px solid #30363d; border-left: 3px solid #f0b429;
    border-radius: 8px; padding: 14px 16px; font-style: italic; color: #c9d1d9;
    margin-top: 8px;
  }}
  .note {{ color: #8b949e; font-size: 0.85rem; margin-top: 6px; }}
  a {{ color: #58a6ff; }}
  kbd {{
    background: #21262d; border: 1px solid #30363d; border-radius: 4px;
    padding: 1px 6px; font-size: 0.85em;
  }}
</style>
</head>
<body>
<h1>Polymarket BTC Backtester</h1>
<p class="tagline">A public MCP server for backtesting resting-limit strategies
on Polymarket's "BTC Up or Down" 5-minute markets, built on 100ms historical
quote data.</p>

<div class="url-box">
  <code id="url">{connector_url}</code>
  <button onclick="navigator.clipboard.writeText(document.getElementById('url').textContent).then(()=>{{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)}})">Copy</button>
</div>
<p class="coverage">Data coverage: {cov_line}</p>

<h2>Add it to Claude</h2>
<ol>
  <li>Open Claude and go to <kbd>Settings</kbd> &rarr; <kbd>Connectors</kbd></li>
  <li>Click <kbd>Add custom connector</kbd></li>
  <li>Paste the URL above and save (no login or API key needed)</li>
  <li>In any chat, open the <kbd>+</kbd> menu and enable the connector</li>
</ol>

<h2>What it does</h2>
<ul>
  <li><strong>backtest</strong> &mdash; simulate a resting limit buy at your
      entry price with a fixed take-profit, one trade per market, across
      ~14,000 historical 5-minute markets. Reports win rate, expectancy,
      drawdown, equity curve, and the exact skew breakdown
      (take-profit exits vs forced $1/$0 resolutions).</li>
  <li><strong>data_coverage</strong> &mdash; the exact date range and any gap days
      behind every result.</li>
  <li><strong>list_markets</strong> / <strong>market_context</strong> /
      <strong>get_price_series</strong> &mdash; browse individual markets, winners,
      price-to-beat, and bid/ask history.</li>
</ul>
<p class="note">Fills are touch-fills at the limit price (maker side, zero
fees). Resolution uses the official market outcomes, never exchange reference
prices. Static historical dataset; nothing here is financial advice.</p>

<h2>Try this prompt</h2>
<div class="prompt">"{EXAMPLE_PROMPT}"</div>

<p class="note" style="margin-top:48px">Data: Kaggle datasets by
<a href="https://www.kaggle.com/datasets/namz8888/polymarket-btc-5-minute-high-frequency-tick-data">namz8888</a>
and
<a href="https://www.kaggle.com/datasets/debayan31415/polymarket-5-minutes-btc-up-down-data">debayan31415</a>.
Personal portfolio project.</p>
</body>
</html>"""
