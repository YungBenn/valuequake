import logging
from datetime import date, datetime, time, timedelta

import httpx
from sqlalchemy import Engine, text

from valuequake.alpaca import NY, fetch_articles, fetch_bars, fetch_dividends
from valuequake.config import Config
from valuequake.daily_job import UPSERT_BARS, UPSERT_DIVIDEND, refresh_calendar, store_articles, store_macro, store_releases

log = logging.getLogger(__name__)

# The free plan refuses SIP data from the last 15 minutes.
SIP_DELAY = timedelta(minutes=16)


def _next_month(month: date) -> date:
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def _months(first_month: date, today: date):
    month = first_month
    while month <= today:
        yield month, _next_month(month)
        month = _next_month(month)


def backfill(engine: Engine, client: httpx.Client, tickers: list[str], first_month: date, now: datetime) -> None:
    """Load 1-min bars per (ticker, month) from first_month to now, plus calendar and dividends.

    A month is recorded as done only once it is over, so the current month is reloaded on every run.
    Raises on the first failed request; running again resumes after the last completed (ticker, month).
    """
    today = now.astimezone(NY).date()
    refresh_calendar(engine, client, first_month - timedelta(days=10), today + timedelta(days=30), today)
    dividends = fetch_dividends(client, tickers, first_month, today + timedelta(days=60))
    with engine.begin() as conn:
        if dividends:
            conn.execute(UPSERT_DIVIDEND, dividends)
        done = {tuple(r) for r in conn.execute(text("SELECT ticker, month FROM backfill_progress"))}

    for month, following in _months(first_month, today):
        start = datetime.combine(month, time(), NY)
        end = min(datetime.combine(following, time(), NY), now - SIP_DELAY)
        for ticker in tickers:
            if (ticker, month) in done:
                continue
            bars = fetch_bars(client, [ticker], start, end)
            with engine.begin() as conn:
                if bars:
                    conn.execute(UPSERT_BARS, bars)
                if following <= today:
                    conn.execute(
                        text("INSERT INTO backfill_progress (ticker, month, bars) VALUES (:t, :m, :n)"),
                        {"t": ticker, "m": month, "n": len(bars)},
                    )
            log.info("backfilled %s %s: %d bars", ticker, month.strftime("%Y-%m"), len(bars))


def backfill_articles(engine: Engine, client: httpx.Client, first_month: date, now: datetime) -> None:
    """Load every Article per month from first_month to now, through the same upsert as the live collector.

    Same resume rules as the bar backfill: the running month is never recorded as done.
    """
    today = now.astimezone(NY).date()
    with engine.connect() as conn:
        done = set(conn.execute(text("SELECT month FROM article_backfill_progress")).scalars())
    for month, following in _months(first_month, today):
        if month in done:
            continue
        start = datetime.combine(month, time(), NY)
        end = min(datetime.combine(following, time(), NY), now)
        with engine.begin() as conn:
            counts = store_articles(conn, fetch_articles(client, start, end))
            if following <= today:
                conn.execute(
                    text("INSERT INTO article_backfill_progress (month, articles) VALUES (:m, :n)"),
                    {"m": month, "n": counts["fetched"]},
                )
        log.info("backfilled Articles %s: %s", month.strftime("%Y-%m"), counts)


def backfill_macro(engine: Engine, client: httpx.Client, config: Config, first_month: date, now: datetime) -> None:
    """FRED series and the Scheduled Release calendar from first_month on; a few requests, so no progress table."""
    today = now.astimezone(NY).date()
    with engine.begin() as conn:
        counts = store_macro(conn, client, config, first_month, today)
        counts |= store_releases(conn, client, config, first_month, today + timedelta(days=60))
    log.info("backfilled macro series and Scheduled Releases: %s", counts)
