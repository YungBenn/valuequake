import os
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from valuequake.clients import get_json

NY = ZoneInfo("America/New_York")
# The calendar lives on the trading API; paper and live keys each need their own host.
TRADING_URL = os.environ.get("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets")


def _pages(client: httpx.Client, url: str, params: dict) -> Iterator[dict]:
    params = dict(params)
    while True:
        body = get_json(client, url, params)
        yield body
        if not body.get("next_page_token"):
            return
        params["page_token"] = body["next_page_token"]


def _fetch(client: httpx.Client, tickers: list[str], start: datetime, end: datetime, adjustment: str) -> dict:
    params = {
        "symbols": ",".join(tickers),
        "timeframe": "1Min",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "adjustment": adjustment,
        "feed": "sip",
        "limit": 10000,
    }
    bars: dict[str, list[dict]] = {}
    for body in _pages(client, "/v2/stocks/bars", params):
        for ticker, rows in (body.get("bars") or {}).items():
            bars.setdefault(ticker, []).extend(rows)
    return bars


def fetch_bars(client: httpx.Client, tickers: list[str], start: datetime, end: datetime) -> list[dict]:
    """All 1-min bars (pre-market, RTH, after hours) in [start, end), split-adjusted plus raw close."""
    adjusted = _fetch(client, tickers, start, end, "split")
    raw = _fetch(client, tickers, start, end, "raw")
    out = []
    for ticker, rows in adjusted.items():
        raw_close = {r["t"]: r["c"] for r in raw.get(ticker, [])}
        for r in rows:
            out.append({
                "ticker": ticker,
                "ts": datetime.fromisoformat(r["t"]),
                "open": r["o"],
                "high": r["h"],
                "low": r["l"],
                "close": r["c"],
                "close_raw": raw_close[r["t"]],
                "volume": r["v"],
                "trade_count": r["n"],
                "vwap": r["vw"],
            })
    return out


def fetch_session_bars(client: httpx.Client, tickers: list[str], session: date) -> list[dict]:
    start = datetime.combine(session, time(), NY)
    return fetch_bars(client, tickers, start, start + timedelta(days=1))


def fetch_dividends(client: httpx.Client, tickers: list[str], start: date, end: date) -> list[dict]:
    """Cash dividends for the tickers between start and end inclusive."""
    params = {
        "symbols": ",".join(tickers),
        "types": "cash_dividend",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 1000,
    }
    return [{
        "id": d["id"],
        "ticker": d["symbol"],
        "ex_date": date.fromisoformat(d["ex_date"]),
        "amount": d["rate"],
        "special": d["special"],
    } for body in _pages(client, "/v1/corporate-actions", params)
      for d in body["corporate_actions"].get("cash_dividends", [])]


def fetch_calendar(client: httpx.Client, start: date, end: date) -> list[dict]:
    """Trading sessions between start and end inclusive, with RTH open and close (half days included)."""
    sessions = get_json(client, f"{TRADING_URL}/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()})
    return [{
        "date": date.fromisoformat(s["date"]),
        "open_at": datetime.combine(date.fromisoformat(s["date"]), time.fromisoformat(s["open"]), NY),
        "close_at": datetime.combine(date.fromisoformat(s["date"]), time.fromisoformat(s["close"]), NY),
    } for s in sessions]


def fetch_articles(client: httpx.Client, start: datetime, end: datetime) -> list[dict]:
    """Every Article published in [start, end)."""
    params = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sort": "asc",
        "include_content": "true",
        "limit": 50,
    }
    return [{
        "id": a["id"],
        "source": a["source"],
        "headline": a["headline"],
        "summary": a["summary"],
        "body": a["content"],
        "symbols": a["symbols"],
        "published_at": datetime.fromisoformat(a["created_at"]),
        "updated_at": datetime.fromisoformat(a["updated_at"]),
    } for body in _pages(client, "/v1beta1/news", params) for a in body["news"]]
