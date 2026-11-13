import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from valuequake.config import load_config
from valuequake.db import apply_schema, make_engine

# The real config with a small placebo sample, so the monthly Base Rate step stays fast in job tests.
_config = load_config("config.toml")
TEST_CONFIG = replace(_config, base_rates=replace(_config.base_rates, placebo_n=50))

FIXTURES = Path(__file__).parent / "fixtures" / "alpaca"
FRED_FIXTURES = Path(__file__).parent / "fixtures" / "fred"


@pytest.fixture(scope="session")
def engine():
    with PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as pg:
        engine = make_engine(pg.get_connection_url())
        apply_schema(engine)
        yield engine
        engine.dispose()


@pytest.fixture
def db(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE bars_1m, articles, trading_sessions, dividends, backfill_progress, article_backfill_progress, macro_series, scheduled_releases, base_rates, job_runs"))
    return engine


PAGE_TOKENS = {"": 1, "U1BZfDIwMjQtMDYtMTBUMTk6NTk6MDBa": 2, "MTcxODAyMzUwMzAwMDAwMDAwMHwzOTIyOTgxNw==": 2}


RECORDED_SESSION = date(2024, 6, 10)


def replay_fred(request: httpx.Request, release_recordings: dict[int, str]) -> httpx.Response:
    """Recorded FRED responses, filtered by date the way FRED filters them."""
    q = request.url.params
    if request.url.path == "/fred/series/observations":
        body = json.loads((FRED_FIXTURES / f"series_{q['series_id']}.json").read_text())
        body["observations"] = [
            o for o in body["observations"] if q["observation_start"] <= o["date"] <= q["observation_end"]
        ]
        return httpx.Response(200, json=body)
    assert request.url.path == "/fred/release/dates"
    release_id = int(q["release_id"])
    body = json.loads((FRED_FIXTURES / f"{release_recordings.get(release_id, f'release_{release_id}')}.json").read_text())
    body["release_dates"] = [r for r in body["release_dates"] if r["date"] >= q["realtime_start"]]
    return httpx.Response(200, json=body)


def recorded_alpaca(
    news: str = "news",
    bars_down_on: frozenset[date] = frozenset(),
    news_down_on: frozenset[date] = frozenset(),
    fred_releases: dict[int, str] | None = None,
) -> httpx.Client:
    """Client serving recorded Alpaca and FRED responses, filtered the way the real APIs filter them.

    Bars and news were recorded for RECORDED_SESSION; windows without it get empty responses.
    `news` picks which news recording to replay; `bars_down_on` / `news_down_on` make requests covering those dates
    fail; `fred_releases` swaps the recording used for a FRED release id.
    """

    def replay(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.stlouisfed.org":
            return replay_fred(request, fred_releases or {})
        q = request.url.params
        if request.url.path == "/v2/calendar":
            days = json.loads((FIXTURES / "calendar.json").read_text())
            return httpx.Response(200, json=[d for d in days if q["start"] <= d["date"] <= q["end"]])
        if request.url.path == "/v1/corporate-actions":
            assert q["types"] == "cash_dividend"
            body = json.loads((FIXTURES / "dividends.json").read_text())
            body["corporate_actions"]["cash_dividends"] = [
                d for d in body["corporate_actions"]["cash_dividends"]
                if d["symbol"] in q["symbols"].split(",") and q["start"] <= d["ex_date"] <= q["end"]
            ]
            return httpx.Response(200, json=body)

        start, end = (datetime.fromisoformat(q[k]).date() for k in ("start", "end"))
        if request.url.path == "/v2/stocks/bars":
            assert q["feed"] == "sip" and q["timeframe"] == "1Min"
            if any(start <= d < end for d in bars_down_on):
                return httpx.Response(500, text="upstream error")
            name = f"bars_{q['adjustment']}_{{page}}.json"
        else:
            assert request.url.path == "/v1beta1/news"
            assert q["include_content"] == "true"
            if any(start <= d < end for d in news_down_on):
                return httpx.Response(500, text="upstream error")
            name = f"{news}_{{page}}.json"
        if not start <= RECORDED_SESSION < end:
            empty = {"bars": {}} if request.url.path == "/v2/stocks/bars" else {"news": []}
            return httpx.Response(200, json=empty | {"next_page_token": None})
        body = json.loads((FIXTURES / name.format(page=PAGE_TOKENS[q.get("page_token", "")])).read_text())
        if "bars" in body:
            body["bars"] = {t: rows for t, rows in body["bars"].items() if t in q["symbols"].split(",")}
        return httpx.Response(200, json=body)

    return httpx.Client(base_url="https://data.alpaca.markets", transport=httpx.MockTransport(replay))


@pytest.fixture
def alpaca():
    return recorded_alpaca()


@pytest.fixture
def calendar(db):
    """Trading calendar stored, as catch-up does before running sessions."""
    from valuequake.daily_job import refresh_calendar

    refresh_calendar(db, recorded_alpaca(), date(2024, 5, 20), date(2024, 7, 12), date(2024, 7, 12))
