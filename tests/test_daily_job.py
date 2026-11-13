import time
from datetime import date, datetime, timezone

import httpx
import pytest
from sqlalchemy import text

from conftest import TEST_CONFIG, recorded_alpaca
from valuequake.daily_job import run_daily_job

SESSION = date(2024, 6, 10)
CONFIG = TEST_CONFIG

pytestmark = pytest.mark.usefixtures("calendar")


def bars(db):
    with db.connect() as conn:
        return conn.execute(text("SELECT * FROM bars_1m ORDER BY ticker, ts")).mappings().all()


def runs(db, step):
    with db.connect() as conn:
        return conn.execute(text("SELECT * FROM job_runs WHERE step = :step ORDER BY id"), {"step": step}).mappings().all()


def articles(db):
    with db.connect() as conn:
        return conn.execute(text("SELECT * FROM articles ORDER BY published_at")).mappings().all()


def test_pgvector_enabled(db):
    with db.connect() as conn:
        assert conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar() == 1


def test_stores_extended_and_rth_bars_with_raw_close(db, alpaca):
    assert run_daily_job(db, alpaca, CONFIG, SESSION)

    rows = bars(db)
    assert [r["ts"] for r in rows] == [
        datetime(2024, 6, 10, h, m, tzinfo=timezone.utc) for h, m in [(8, 0), (13, 30), (19, 59), (20, 0), (23, 59)]
    ]
    first = dict(rows[0])
    assert first == {
        "ticker": "SPY", "ts": rows[0]["ts"], "open": 534.10, "high": 534.25, "low": 534.05,
        "close": 534.20, "close_raw": 1534.20, "volume": 4120, "trade_count": 57, "vwap": 534.16,
    }

    [run] = runs(db, "bars")
    assert (run["session_date"], run["step"], run["status"], run["counts"], run["error"]) == (
        SESSION, "bars", "ok", {"SPY": 5}, None,
    )


def test_rerun_leaves_bars_unchanged(db, alpaca):
    run_daily_job(db, alpaca, CONFIG, SESSION)
    before = bars(db)
    run_daily_job(db, alpaca, CONFIG, SESSION)
    assert bars(db) == before
    assert [r["status"] for r in runs(db, "bars")] == ["ok", "ok"]


def test_alpaca_failure_is_recorded(db):
    down = httpx.Client(
        base_url="https://data.alpaca.markets",
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="upstream error")),
    )
    assert not run_daily_job(db, down, CONFIG, SESSION)

    assert bars(db) == [] and articles(db) == []
    for step in ("bars", "articles"):
        [run] = runs(db, step)
        assert run["status"] == "failed"
        assert "500" in run["error"]


def test_stores_all_articles_in_window(db, alpaca):
    assert run_daily_job(db, alpaca, CONFIG, SESSION)

    rows = articles(db)
    # Saturday's Article lands in Monday's session; untagged and empty-body Articles are kept.
    assert [r["id"] for r in rows] == [39221044, 39229817, 39236602]
    assert dict(rows[1]) == {
        "id": 39229817,
        "source": "benzinga",
        "headline": "Apple Kicks Off WWDC With AI Push Expected",
        "summary": "Apple's developer conference starts today with investors watching for generative AI features.",
        "body": "<p>Apple's developer conference starts today with investors watching for generative AI features.</p>",
        "symbols": ["AAPL", "QQQ"],
        "published_at": datetime(2024, 6, 10, 12, 45, 3, tzinfo=timezone.utc),
        "updated_at": datetime(2024, 6, 10, 12, 45, 4, tzinfo=timezone.utc),
    }
    [run] = runs(db, "articles")
    assert (run["status"], run["counts"]) == ("ok", {"fetched": 3, "inserted": 3, "updated": 0})


def test_rerun_creates_no_duplicate_articles(db, alpaca):
    run_daily_job(db, alpaca, CONFIG, SESSION)
    before = articles(db)
    run_daily_job(db, alpaca, CONFIG, SESSION)
    assert articles(db) == before
    assert runs(db, "articles")[-1]["counts"] == {"fetched": 3, "inserted": 0, "updated": 0}


def test_updated_article_keeps_original_published_at(db, alpaca):
    run_daily_job(db, alpaca, CONFIG, SESSION)
    run_daily_job(db, recorded_alpaca(news="news_updated"), CONFIG, SESSION)

    apple = next(r for r in articles(db) if r["id"] == 39229817)
    assert apple["headline"] == "Apple Unveils 'Apple Intelligence' At WWDC"
    assert apple["body"] == "<p>Apple unveiled Apple Intelligence and a ChatGPT integration at WWDC.</p>"
    assert apple["updated_at"] == datetime(2024, 6, 10, 18, 2, 10, tzinfo=timezone.utc)
    assert apple["published_at"] == datetime(2024, 6, 10, 12, 45, 3, tzinfo=timezone.utc)
    assert runs(db, "articles")[-1]["counts"] == {"fetched": 3, "inserted": 0, "updated": 1}


def test_article_window_starts_after_previous_session(db):
    windows = []
    client = recorded_alpaca()
    client.event_hooks["request"] = [
        lambda r: windows.append((r.url.params["start"], r.url.params["end"])) if r.url.path == "/v1beta1/news" else None
    ]
    # 2024-06-19 is Juneteenth, so the session after 06-18 is 06-20 and its window covers the holiday.
    run_daily_job(db, client, CONFIG, date(2024, 6, 20))
    assert windows == [("2024-06-19T00:00:00-04:00", "2024-06-21T00:00:00-04:00")]


def test_dividends_refreshed_for_session(db, alpaca):
    run_daily_job(db, alpaca, CONFIG, SESSION)
    with db.connect() as conn:
        rows = conn.execute(text("SELECT ticker, ex_date, amount FROM dividends ORDER BY ex_date")).all()
    # Window is 30 days back to 60 days ahead of the session.
    assert rows == [("TLT", date(2024, 6, 3), 0.3149), ("SPY", date(2024, 6, 21), 1.7593), ("TLT", date(2024, 7, 1), 0.3266)]
    assert runs(db, "dividends")[-1]["counts"] == {"dividends": 3}


def rate_limited(first: httpx.Response) -> tuple[httpx.Client, list[float]]:
    """Recorded Alpaca whose first data request gets `first` instead of the recorded answer."""
    inner = recorded_alpaca()._transport
    sent = []

    def handler(request):
        sent.append(time.time())
        return first if len(sent) == 1 else inner.handle_request(request)

    return httpx.Client(base_url="https://data.alpaca.markets", transport=httpx.MockTransport(handler)), sent


def test_429_waits_for_reset_and_retries(db):
    reset = int(time.time()) + 1
    client, sent = rate_limited(httpx.Response(429, headers={"X-RateLimit-Reset": str(reset)}))
    assert run_daily_job(db, client, CONFIG, SESSION)
    assert sent[1] >= reset
    assert len(bars(db)) == 5


def test_exhausted_rate_limit_waits_before_next_request(db):
    reset = time.time() + 1
    exhausted = httpx.Response(
        200, json={"bars": {}, "next_page_token": None},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
    )
    client, sent = rate_limited(exhausted)
    run_daily_job(db, client, CONFIG, SESSION)
    assert sent[1] >= reset


def table(db, sql):
    with db.connect() as conn:
        return conn.execute(text(sql)).all()


def test_macro_series_stored_for_session_and_days_before(db, alpaca):
    run_daily_job(db, alpaca, CONFIG, SESSION)
    rows = table(db, "SELECT series_id, date, value FROM macro_series ORDER BY series_id, date")
    # Ten days back from 06-10; FRED's '.' (Memorial Day) is skipped.
    assert [(s, d) for s, d, _ in rows if s == "VIXCLS"] == [
        ("VIXCLS", date(2024, 5, d)) for d in (31,)] + [("VIXCLS", date(2024, 6, d)) for d in (3, 4, 5, 6, 7, 10)]
    assert ("DGS10", date(2024, 6, 10), 4.47) in rows
    assert runs(db, "macro")[-1]["counts"] == {"VIXCLS": 7, "DGS10": 7}


def test_scheduled_releases_in_utc_with_fomc_from_config(db, alpaca):
    run_daily_job(db, alpaca, CONFIG, SESSION)
    rows = table(db, "SELECT name, released_at, source FROM scheduled_releases ORDER BY released_at")
    # Window is 7 days back to 60 days ahead of 06-10; 08:30 and 14:00 ET are 12:30 and 18:00 UTC in summer.
    assert rows[:4] == [
        ("NFP", datetime(2024, 6, 7, 12, 30, tzinfo=timezone.utc), "fred:50"),
        ("CPI", datetime(2024, 6, 12, 12, 30, tzinfo=timezone.utc), "fred:10"),
        ("FOMC", datetime(2024, 6, 12, 18, 0, tzinfo=timezone.utc), "config"),
        ("GDP", datetime(2024, 6, 27, 12, 30, tzinfo=timezone.utc), "fred:53"),
    ]
    assert runs(db, "releases")[-1]["counts"] == {"CPI": 2, "NFP": 3, "GDP": 2, "PCE": 2, "FOMC": 2}


def test_rerun_leaves_macro_and_releases_unchanged(db, alpaca):
    run_daily_job(db, alpaca, CONFIG, SESSION)
    before = table(db, "SELECT * FROM macro_series ORDER BY 1, 2"), table(db, "SELECT * FROM scheduled_releases ORDER BY 1, 2")
    run_daily_job(db, alpaca, CONFIG, SESSION)
    assert (table(db, "SELECT * FROM macro_series ORDER BY 1, 2"), table(db, "SELECT * FROM scheduled_releases ORDER BY 1, 2")) == before


def test_rescheduled_release_replaces_the_old_date(db, alpaca):
    run_daily_job(db, alpaca, CONFIG, SESSION)
    run_daily_job(db, recorded_alpaca(fred_releases={10: "release_10_rescheduled"}), CONFIG, SESSION)
    cpi = table(db, "SELECT released_at FROM scheduled_releases WHERE name = 'CPI' ORDER BY 1")
    assert [r[0].date() for r in cpi] == [date(2024, 6, 13), date(2024, 7, 11)]
