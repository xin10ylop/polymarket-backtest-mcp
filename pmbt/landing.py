"""The landing page served at /. Same design as the static /landing page,
with the connector URL and coverage numbers filled in by the server."""

import json
import os

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "landing_template.html")
with open(_TEMPLATE_PATH, encoding="utf-8") as f:
    _TEMPLATE = f.read()

GITHUB_URL = "https://github.com/xin10ylop/polymarket-backtest-mcp"


def landing_html(connector_url: str, coverage: dict) -> str:
    markets = coverage.get("tradeable_markets") or 14361
    days = coverage.get("total_days_covered") or 52
    html = _TEMPLATE.replace("__CONNECTOR_URL__", json.dumps(connector_url))
    html = html.replace("__MARKETS__", f"{markets:,}")
    html = html.replace("__DAYS__", str(days))
    return html
