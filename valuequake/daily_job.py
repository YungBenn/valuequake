import json
import logging
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone

import httpx
from sqlalchemy import Connection, Engine, text

from valuequake.alpaca import NY, fetch_articles, fetch_calendar, fetch_dividends, fetch_session_bars
from valuequake.base_rates import refresh_base_rates
from valuequake.config import Config
from valuequake.fred import fetch_release_dates, fetch_series

log = logging.getLogger(__name__)

# After hours end 4h after the RTH close (20:00 ET, 17:00 on half days); the margin covers SIP's 15-min delay.
SESSION_READY_AFTER_CLOSE = timedelta(hours=4, minutes=30)

UPSERT_BARS = text("""
    INSERT INTO bars_1m (ticker, ts, open, high, low, close, close_raw, volume, trade_count, vwap)
    VALUES (:ticker, :ts, :open, :high, :low, :close, :close_raw, :volume, :trade_count, :vwap)
    ON CONFLICT (ticker, ts) DO UPDATE SET
        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
        close_raw = EXCLUDED.close_raw, volume = EXCLUDED.volume,
        trade_count = EXCLUDED.trade_count, vwap = EXCLUDED.vwap
""")

UPSERT_DIVIDEND = text("""
    INSERT INTO dividends (id, ticker, ex_date, amount, special) VALUES (:id, :ticker, :ex_date, :amount, :special)
    ON CONFLICT (id) DO UPDATE SET
        ticker = EXCLUDED.ticker, ex_date = EXCLUDED.ex_date, amount = EXCLUDED.amount, special = EXCLUDED.special
""")

# Only a newer version replaces content, and published_at is never touched.
UPSERT_ARTICLE = text("""
    INSERT INTO articles (id, source, headline, summary, body, symbols, published_at, updated_at)
    VALUES (:id, :source, :headline, :summary, :body, :symbols, :published_at, :updated_at)
    ON CONFLICT (id) DO UPDATE SET
        source = EXCLUDED.source, headline = EXCLUDED.headline, summary = EXCLUDED.summary,
        body = EXCLUDED.body, symbols = EXCLUDED.symbols, updated_at = EXCLUDED.updated_at
    WHERE EXCLUDED.updated_at > articles.updated_at
    RETURNING xmax = 0 AS inserted
""")

UPSERT_MACRO = text("""
    INSERT INTO macro_series (series_id, date, value) VALUES (:series_id, :date, :value)
    ON CONFLICT (series_id, date) DO UPDATE SET value = EXCLUDED.value
""")

UPSERT_SESSION = text("""
    INSERT INTO trading_sessions (date, open_at, close_at) VALUES (:date, :open_at, :close_at)
    ON CONFLICT (date) DO UPDATE SET open_at = EXCLUDED.open_at, close_at = EXCLUDED.close_at
""")

INSERT_RUN = text("""
    INSERT INTO job_runs (session_date, step, status, counts, error, started_at, finished_at)
    VALUES (:session_date, :step, :status, CAST(:counts AS jsonb), :error, :started_at, now())
""")


def _bars(conn: Connection, client: httpx.Client, config: Config, session: date) -> dict:
    bars = fetch_session_bars(client, config.tickers, session)
    if bars:
        conn.execute(UPSERT_BARS, bars)
    return dict(Counter(bar["ticker"] for bar in bars))


def _dividends(conn: Connection, client: httpx.Client, config: Config, session: date) -> dict:
    # Wide window: dividends are announced weeks ahead and corrections can land after the ex-date.
    dividends = fetch_dividends(client, config.tickers, session - timedelta(days=30), session + timedelta(days=60))
    if dividends:
        conn.execute(UPSERT_DIVIDEND, dividends)
    return {"dividends": len(dividends)}


def _articles(conn: Connection, client: httpx.Client, config: Config, session: date) -> dict:
    # From the day after the previous session, so weekend and holiday Articles land in the next session.
    previous = conn.execute(
        text("SELECT max(date) FROM trading_sessions WHERE date < :d"), {"d": session}
    ).scalar()
    if previous is None:
        raise RuntimeError(f"no trading session before {session} in the stored calendar")
    start = datetime.combine(previous + timedelta(days=1), time(), NY)
    end = datetime.combine(session + timedelta(days=1), time(), NY)
    return store_articles(conn, fetch_articles(client, start, end))


def store_articles(conn: Connection, articles: list[dict]) -> dict:
    counts = Counter(fetched=len(articles), inserted=0, updated=0)
    for article in articles:
        inserted = conn.execute(UPSERT_ARTICLE, article).scalar()
        if inserted is not None:
            counts["inserted" if inserted else "updated"] += 1
    return dict(counts)


def store_macro(conn: Connection, client: httpx.Client, config: Config, start: date, end: date) -> dict:
    counts = {}
    for series_id in config.macro_series:
        rows = fetch_series(client, series_id, start, end)
        if rows:
            conn.execute(UPSERT_MACRO, rows)
        counts[series_id] = len(rows)
    return counts


def _macro(conn: Connection, client: httpx.Client, config: Config, session: date) -> dict:
    # FRED publishes some series a business day late, so each run also fills the days before.
    return store_macro(conn, client, config, session - timedelta(days=10), session)


def store_releases(conn: Connection, client: httpx.Client, config: Config, start: date, end: date) -> dict:
    """Replace every Scheduled Release between start and end, so a rescheduled release does not linger."""
    r = config.releases
    rows = [
        {"name": name, "released_at": datetime.combine(d, r.fred_time_et, NY), "source": f"fred:{release_id}"}
        for name, release_id in r.fred.items()
        for d in fetch_release_dates(client, release_id, start, end)
    ] + [
        {"name": "FOMC", "released_at": datetime.combine(d, r.fomc_time_et, NY), "source": "config"}
        for d in r.fomc if start <= d <= end
    ]
    conn.execute(
        text("DELETE FROM scheduled_releases WHERE released_at >= :start AND released_at < :end"),
        {"start": datetime.combine(start, time(), NY), "end": datetime.combine(end + timedelta(days=1), time(), NY)},
    )
    if rows:
        conn.execute(text("INSERT INTO scheduled_releases (name, released_at, source) VALUES (:name, :released_at, :source)"), rows)
    return dict(Counter(row["name"] for row in rows))


def _releases(conn: Connection, client: httpx.Client, config: Config, session: date) -> dict:
    # Ahead of the session too: the calendar of upcoming releases matters before they happen.
    return store_releases(conn, client, config, session - timedelta(days=7), session + timedelta(days=60))


def _base_rates(conn: Connection, client: httpx.Client, config: Config, session: date) -> dict:
    # Monthly: skip when this label_version already has Base Rates from this month or later.
    latest = conn.execute(
        text("SELECT max(window_end) FROM base_rates WHERE label_version = :v"), {"v": config.outcome.label_version}
    ).scalar()
    if latest is not None and (latest >= session or (latest.year, latest.month) == (session.year, session.month)):
        return {"skipped": "up to date for this month"}
    return refresh_base_rates(conn, config, session)


STEPS: list[tuple[str, Callable[..., dict]]] = [
    ("bars", _bars), ("dividends", _dividends), ("articles", _articles), ("macro", _macro), ("releases", _releases),
    ("base_rates", _base_rates),
]


def _log_run(engine: Engine, session: date, step: str, started_at: datetime, counts: dict, error: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(INSERT_RUN, {
            "session_date": session,
            "step": step,
            "status": "failed" if error else "ok",
            "counts": json.dumps(counts),
            "error": error,
            "started_at": started_at,
        })


def run_daily_job(engine: Engine, client: httpx.Client, config: Config, session: date) -> bool:
    """Run every step for one session, logging a job run per step. Returns True if all succeeded."""
    ok = True
    for step, fn in STEPS:
        started_at = datetime.now(timezone.utc)
        counts, error = {}, None
        try:
            with engine.begin() as conn:
                counts = fn(conn, client, config, session)
        except Exception as exc:
            log.exception("%s step failed for %s", step, session)
            error = repr(exc)
            ok = False
        _log_run(engine, session, step, started_at, counts, error)
    return ok


def refresh_calendar(engine: Engine, client: httpx.Client, start: date, end: date, today: date) -> None:
    started_at = datetime.now(timezone.utc)
    try:
        sessions = fetch_calendar(client, start, end)
        if sessions:
            with engine.begin() as conn:
                conn.execute(UPSERT_SESSION, sessions)
        _log_run(engine, today, "calendar", started_at, {"sessions": len(sessions)}, None)
    except Exception as exc:
        log.exception("calendar refresh failed")
        _log_run(engine, today, "calendar", started_at, {}, repr(exc))


PENDING_SESSIONS = text("""
    WITH done AS (
        SELECT session_date FROM job_runs
        WHERE status = 'ok' AND step = ANY(:steps)
        GROUP BY session_date HAVING count(DISTINCT step) = cardinality(:steps)
    ),
    ready AS (SELECT date FROM trading_sessions WHERE close_at + :ready_after <= :now),
    anchor AS (
        -- The first session ever attempted, or just the latest ready session on a fresh install.
        SELECT coalesce(
            (SELECT min(session_date) FROM job_runs WHERE step = ANY(:steps)),
            (SELECT max(date) FROM ready)
        ) AS date
    )
    SELECT date FROM ready
    WHERE date >= (SELECT date FROM anchor) AND date NOT IN (SELECT session_date FROM done)
    ORDER BY date
""")


def pending_sessions(engine: Engine, now: datetime) -> list[date]:
    """Ready sessions since the first attempted one that have not had every step succeed."""
    with engine.connect() as conn:
        return list(conn.execute(PENDING_SESSIONS, {
            "steps": [step for step, _ in STEPS], "ready_after": SESSION_READY_AFTER_CLOSE, "now": now,
        }).scalars())


def catch_up(engine: Engine, client: httpx.Client, config: Config, now: datetime) -> None:
    """Run the daily job, in date order, for every ready session not yet done."""
    today = now.astimezone(NY).date()
    with engine.connect() as conn:
        anchor, covered_until = conn.execute(text("""
            SELECT (SELECT min(session_date) FROM job_runs WHERE step = ANY(:steps)),
                   (SELECT max(date) FROM trading_sessions)
        """), {"steps": [step for step, _ in STEPS]}).one()
    # Refresh only when something may be due or the stored calendar runs out, so idle ticks stay silent.
    if pending_sessions(engine, now) or covered_until is None or covered_until < today:
        # Reach back far enough to know the session before the anchor (the articles window needs it).
        refresh_calendar(engine, client, (anchor or today) - timedelta(days=10), today + timedelta(days=30), today)
    for session in pending_sessions(engine, now):
        run_daily_job(engine, client, config, session)
