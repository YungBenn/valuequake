from datetime import date, datetime

import pytest
from sqlalchemy import text

from conftest import TEST_CONFIG, recorded_alpaca
from valuequake.alpaca import NY
from valuequake.backfill import backfill, backfill_articles, backfill_macro
from valuequake.config import load_config
from valuequake.daily_job import refresh_calendar, run_daily_job
from valuequake.gaps import bar_gaps

TICKERS = ["SPY", "TLT"]
MAY = date(2024, 5, 1)
NOW = datetime(2024, 7, 15, 12, tzinfo=NY)


def query(db, sql):
    with db.connect() as conn:
        return conn.execute(text(sql)).all()


def bar_requests(client) -> list[tuple[str, str]]:
    """Record (symbols, month) of each split-adjusted bar request made through the client."""
    seen = []
    client.event_hooks["request"] = [
        lambda r: seen.append((r.url.params["symbols"], r.url.params["start"][:7]))
        if r.url.path == "/v2/stocks/bars" and r.url.params["adjustment"] == "split" and "page_token" not in r.url.params
        else None
    ]
    return seen


def test_backfill_loads_bars_dividends_and_progress(db):
    backfill(db, recorded_alpaca(), TICKERS, MAY, NOW)

    assert query(db, "SELECT ticker, count(*), min(close_raw) FROM bars_1m GROUP BY ticker") == [("SPY", 5, 1533.62)]
    assert query(db, "SELECT ticker, ex_date, amount FROM dividends ORDER BY ex_date") == [
        ("TLT", date(2024, 6, 3), 0.3149), ("SPY", date(2024, 6, 21), 1.7593), ("TLT", date(2024, 7, 1), 0.3266),
    ]
    # July is still running, so it is not recorded as done.
    assert query(db, "SELECT ticker, month, bars FROM backfill_progress ORDER BY month, ticker") == [
        ("SPY", MAY, 0), ("TLT", MAY, 0), ("SPY", date(2024, 6, 1), 5), ("TLT", date(2024, 6, 1), 0),
    ]


def test_interrupted_backfill_resumes_after_last_completed_month(db):
    with pytest.raises(Exception, match="500"):
        backfill(db, recorded_alpaca(bars_down_on=frozenset({date(2024, 6, 10)})), TICKERS, MAY, NOW)
    assert query(db, "SELECT ticker, month FROM backfill_progress ORDER BY ticker") == [("SPY", MAY), ("TLT", MAY)]

    client = recorded_alpaca()
    requests = bar_requests(client)
    backfill(db, client, TICKERS, MAY, NOW)

    assert requests == [("SPY", "2024-06"), ("TLT", "2024-06"), ("SPY", "2024-07"), ("TLT", "2024-07")]
    assert query(db, "SELECT count(*) FROM bars_1m") == [(5,)]


def test_rerun_only_reloads_the_running_month(db):
    backfill(db, recorded_alpaca(), TICKERS, MAY, NOW)
    client = recorded_alpaca()
    requests = bar_requests(client)
    backfill(db, client, TICKERS, MAY, NOW)
    assert requests == [("SPY", "2024-07"), ("TLT", "2024-07")]


def test_gap_check_lists_missing_and_partial_sessions(db):
    backfill(db, recorded_alpaca(), TICKERS, MAY, NOW)
    gaps = bar_gaps(db, NOW)

    # SPY's fixture has 2 RTH bars on its first session; every later session has none.
    assert gaps[0] == ("SPY", date(2024, 6, 10), 2, 390)
    assert ("SPY", date(2024, 7, 3), 0, 210) in gaps  # half day
    assert {d for _, d, _, _ in gaps}.isdisjoint({date(2024, 6, 19), date(2024, 7, 4), date(2024, 6, 7)})
    assert all(t == "SPY" for t, *_ in gaps)  # TLT has no bars at all, so no span to check
    assert gaps[-1][1] == date(2024, 7, 12)  # 2024-07-15 is still running at NOW


def article_months(client) -> list[str]:
    """Record the month of each first-page news request made through the client."""
    seen = []
    client.event_hooks["request"] = [
        lambda r: seen.append(r.url.params["start"][:7])
        if r.url.path == "/v1beta1/news" and "page_token" not in r.url.params else None
    ]
    return seen


def test_article_backfill_is_idempotent_with_live_collector(db):
    backfill_articles(db, recorded_alpaca(), MAY, NOW)
    assert query(db, "SELECT id FROM articles ORDER BY id") == [(39221044,), (39229817,), (39236602,)]
    assert query(db, "SELECT month, articles FROM article_backfill_progress ORDER BY month") == [
        (MAY, 0), (date(2024, 6, 1), 3),
    ]

    # The live collector then sees the same Articles and adds nothing.
    refresh_calendar(db, recorded_alpaca(), MAY, date(2024, 7, 12), date(2024, 7, 12))
    run_daily_job(db, recorded_alpaca(), TEST_CONFIG, date(2024, 6, 10))
    assert query(db, "SELECT count(*) FROM articles") == [(3,)]
    assert query(db, "SELECT counts FROM job_runs WHERE step = 'articles'") == [
        ({"fetched": 3, "inserted": 0, "updated": 0},),
    ]


def test_interrupted_article_backfill_resumes_after_last_completed_month(db):
    with pytest.raises(Exception, match="500"):
        backfill_articles(db, recorded_alpaca(news_down_on=frozenset({date(2024, 6, 10)})), MAY, NOW)
    assert query(db, "SELECT month FROM article_backfill_progress") == [(MAY,)]
    assert query(db, "SELECT count(*) FROM articles") == [(0,)]

    client = recorded_alpaca()
    months = article_months(client)
    backfill_articles(db, client, MAY, NOW)
    assert months == ["2024-06", "2024-07"]
    assert query(db, "SELECT count(*) FROM articles") == [(3,)]

    client = recorded_alpaca()
    months = article_months(client)
    backfill_articles(db, client, MAY, NOW)
    assert months == ["2024-07"]


def test_macro_backfill_covers_series_and_release_calendar(db):
    backfill_macro(db, recorded_alpaca(), load_config("config.toml"), MAY, NOW)
    assert query(db, "SELECT series_id, count(*) FROM macro_series GROUP BY 1 ORDER BY 1") == [("DGS10", 11), ("VIXCLS", 12)]  # minus FRED's "." days
    # 05-01 through 09-13 (60 days past NOW): late-September GDP and PCE fall outside, FOMC 05-01 inside.
    assert query(db, "SELECT name, count(*) FROM scheduled_releases GROUP BY 1 ORDER BY 1") == [
        ("CPI", 5), ("FOMC", 3), ("GDP", 4), ("NFP", 5), ("PCE", 4),
    ]
