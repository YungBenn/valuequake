from datetime import datetime

from sqlalchemy import Engine, text

from valuequake.daily_job import SESSION_READY_AFTER_CLOSE

# Sessions since each ticker's first bar whose RTH bar count is below min_coverage of the RTH minutes.
# A 1-min bar exists only for minutes with trades, so thin tickers (UUP) fall short more often than SPY.
BAR_GAPS = text("""
    WITH span AS (SELECT ticker, min(ts) AS first_ts FROM bars_1m GROUP BY ticker)
    SELECT span.ticker, s.date, count(b.ts) AS rth_bars,
           (extract(epoch FROM s.close_at - s.open_at) / 60)::int AS expected
    FROM span
    JOIN trading_sessions s ON s.close_at > span.first_ts AND s.close_at + :ready_after <= :now
    LEFT JOIN bars_1m b ON b.ticker = span.ticker AND b.ts >= s.open_at AND b.ts < s.close_at
    GROUP BY span.ticker, s.date, s.open_at, s.close_at
    HAVING count(b.ts) < :min_coverage * extract(epoch FROM s.close_at - s.open_at) / 60
    ORDER BY span.ticker, s.date
""")


def bar_gaps(engine: Engine, now: datetime, min_coverage: float = 0.95) -> list[tuple]:
    """(ticker, session date, RTH bars, expected RTH bars) for every session with missing or partial bars."""
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(BAR_GAPS, {
            "ready_after": SESSION_READY_AFTER_CLOSE, "now": now, "min_coverage": min_coverage,
        })]
