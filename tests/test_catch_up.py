from datetime import date, datetime

from sqlalchemy import text

from conftest import TEST_CONFIG, recorded_alpaca
from valuequake.alpaca import NY
from valuequake.daily_job import catch_up

CONFIG = TEST_CONFIG


def at(day: int, month: int = 6, hour: int = 21) -> datetime:
    return datetime(2024, month, day, hour, tzinfo=NY)


def sessions_run(db) -> list[date]:
    with db.connect() as conn:
        return list(conn.execute(text("SELECT session_date FROM job_runs WHERE step = 'bars' ORDER BY id")).scalars())


def test_calendar_stored_with_half_days(db):
    catch_up(db, recorded_alpaca(), CONFIG, at(12))
    with db.connect() as conn:
        rows = dict(conn.execute(text("SELECT date, close_at FROM trading_sessions")).all())
    assert rows[date(2024, 7, 3)] == datetime(2024, 7, 3, 13, tzinfo=NY)
    assert rows[date(2024, 7, 5)] == datetime(2024, 7, 5, 16, tzinfo=NY)
    assert date(2024, 7, 4) not in rows and date(2024, 6, 19) not in rows


def test_first_start_runs_only_latest_ready_session(db):
    catch_up(db, recorded_alpaca(), CONFIG, at(12))
    assert sessions_run(db) == [date(2024, 6, 12)]


def test_three_session_gap_caught_up_in_order(db):
    catch_up(db, recorded_alpaca(), CONFIG, at(12))
    # Asleep from Wednesday night until Tuesday noon: Thu, Fri, Mon are missed; Tuesday isn't over yet.
    catch_up(db, recorded_alpaca(), CONFIG, at(18, hour=12))
    assert sessions_run(db) == [date(2024, 6, d) for d in (12, 13, 14, 17)]


def test_weekends_and_holidays_are_not_missed_sessions(db):
    catch_up(db, recorded_alpaca(), CONFIG, at(17))
    catch_up(db, recorded_alpaca(), CONFIG, at(22, hour=12))
    assert sessions_run(db) == [date(2024, 6, d) for d in (17, 18, 20, 21)]


def test_nothing_due_is_a_no_op(db):
    catch_up(db, recorded_alpaca(), CONFIG, at(12))
    with db.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM job_runs")).scalar()
    catch_up(db, recorded_alpaca(), CONFIG, at(12, hour=22))
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM job_runs")).scalar() == before


def test_failed_session_is_retried_even_after_later_success(db):
    catch_up(db, recorded_alpaca(), CONFIG, at(12))
    catch_up(db, recorded_alpaca(bars_down_on=frozenset({date(2024, 6, 13)})), CONFIG, at(14))
    catch_up(db, recorded_alpaca(), CONFIG, at(14, hour=22))
    assert sessions_run(db) == [date(2024, 6, d) for d in (12, 13, 14, 13)]
    with db.connect() as conn:
        statuses = conn.execute(text(
            "SELECT session_date, status FROM job_runs WHERE step = 'bars' ORDER BY id"
        )).all()
    assert [s for _, s in statuses] == ["ok", "failed", "ok", "ok"]
