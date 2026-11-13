from dataclasses import replace
from datetime import date, datetime, time

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from conftest import TEST_CONFIG, recorded_alpaca
from valuequake.base_rates import aggregate, placebo_outcomes, placebo_times
from valuequake.config import load_config
from valuequake.daily_job import run_daily_job
from valuequake.market_data import MarketView

PARAMS = load_config("config.toml").outcome
NY = "America/New_York"


def et(d: date, hh: int, mm: int = 0) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, time(hh, mm)), tz=NY).tz_convert("UTC")


def sessions_between(start: str, end: str) -> pd.DataFrame:
    days = [d.date() for d in pd.bdate_range(start, end)]
    return pd.DataFrame({"date": days, "open_at": [et(d, 9, 30) for d in days], "close_at": [et(d, 16) for d in days]})


def spy_bars(sessions: pd.DataFrame, daily_vol: float = 0.01) -> pd.DataFrame:
    """SPY whose log price is Brownian over RTH minutes (no overnight gap), so every window's return is normal with
    variance proportional to its RTH minutes: |SAR| is |N(0, 1)| up to sigma estimation error."""
    rng = np.random.default_rng(3)
    minutes = pd.DatetimeIndex(np.concatenate([
        pd.date_range(o, c, freq="1min", inclusive="left") for o, c in zip(sessions["open_at"], sessions["close_at"])
    ]))
    steps = rng.normal(0, daily_vol / np.sqrt(390), len(minutes))
    return pd.DataFrame({"close": 100 * np.exp(np.cumsum(steps)), "volume": 1000}, index=minutes)


SESSIONS = sessions_between("2023-01-02", "2025-06-30")
BARS = spy_bars(SESSIONS)
VIEW = MarketView(SESSIONS, {"SPY": BARS})
START, END = et(date(2024, 3, 1), 0), et(date(2025, 6, 27), 16)  # leaves 250+ sessions of history before START
TIMES = placebo_times(START, END, 3000, np.random.default_rng(11))
OUTCOMES, UNAVAILABLE = placebo_outcomes("SPY", VIEW, PARAMS, TIMES)


def shares(rows: pd.DataFrame) -> pd.Series:
    totals = rows[["low", "medium", "high"]].sum()
    return totals / totals.sum()


def test_placebo_times_cover_the_window_and_repeat_for_a_seed():
    assert TIMES.min() >= START and TIMES.max() < END and TIMES.is_monotonic_increasing
    assert (placebo_times(START, END, 3000, np.random.default_rng(11)) == TIMES).all()


def test_timing_buckets_sampled_in_proportion_to_calendar_time():
    assert UNAVAILABLE == 0
    timing = pd.Series([o.timing for o in OUTCOMES]).value_counts(normalize=True)
    week = 7 * 24
    expected = {"rth": 5 * 6.5 / week, "pre_market": 5 * 9.5 / week, "after_close": 5 * 8 / week, "weekend": 48 / week}
    for bucket, share in expected.items():
        assert timing[bucket] == pytest.approx(share, abs=0.03)


def test_normal_returns_give_normal_label_shares():
    # P(|Z| < 1) = 0.683, P(1 <= |Z| < 2) = 0.272, P(|Z| >= 2) = 0.046; EWMA sigma error fattens the tails a bit.
    s = shares(aggregate(OUTCOMES))
    assert s["low"] == pytest.approx(0.683, abs=0.035)
    assert s["medium"] == pytest.approx(0.272, abs=0.035)
    assert s["high"] == pytest.approx(0.046, abs=0.02)


def test_aggregate_splits_by_timing_and_release_in_window():
    releases = [et(d, 8, 30) for d in SESSIONS["date"][::10]]  # a release every tenth session
    view = MarketView(SESSIONS, {"SPY": BARS}, releases=releases)
    outcomes, _ = placebo_outcomes("SPY", view, PARAMS, TIMES[:600])
    rows = aggregate(outcomes)
    assert set(zip(rows.timing, rows.release_in_window)) == {
        (t, r) for t in ["after_close", "pre_market", "rth", "weekend"] for r in (False, True)
    } - {("rth", True)}  # an 08:30 release is never inside an RTH window (t0 is after the open)
    assert (rows[["low", "medium", "high"]].sum(axis=1) == rows.n).all() and rows.n.sum() == 600


# Daily job: monthly refresh, against Postgres.
def seed_history(db, tickers=("SPY", "QQQ")):
    """Calendar plus one RTH close bar per session per ticker, 2022-06 to 2024-05, away from the recorded session."""
    sessions = sessions_between("2022-06-01", "2024-05-31")
    rng = np.random.default_rng(9)
    rows = []
    for t in tickers:
        closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(sessions))))
        rows += [{"t": t, "ts": c - pd.Timedelta(minutes=1), "p": float(p)} for c, p in zip(sessions["close_at"], closes)]
    with db.begin() as conn:
        conn.execute(text("INSERT INTO trading_sessions VALUES (:date, :open_at, :close_at)"), sessions.to_dict("records"))
        conn.execute(text("INSERT INTO bars_1m VALUES (:t, :ts, :p, :p, :p, :p, :p, 1000, 10, :p)"), rows)


def test_daily_job_recomputes_base_rates_once_per_month(db):
    seed_history(db)
    config = replace(TEST_CONFIG, targets=["SPY", "QQQ"], sensitivity=[],
                     base_rates=replace(TEST_CONFIG.base_rates, placebo_n=200, history_years=1))
    for session in [date(2024, 4, 15), date(2024, 4, 22), date(2024, 5, 6), date(2024, 4, 30)]:
        assert run_daily_job(db, recorded_alpaca(), config, session)

    with db.connect() as conn:
        stored = conn.execute(text(
            "SELECT etf, window_start, window_end, label_version, sum(n), sum(low + medium + high) "
            "FROM base_rates GROUP BY 1, 2, 3, 4 ORDER BY 3, 1"
        )).all()
        runs = conn.execute(text("SELECT session_date, counts FROM job_runs WHERE step = 'base_rates' ORDER BY id")).all()
    # 04-22 is the same month and 04-30 comes after a newer refresh, so both are skipped.
    assert [(etf, start, end) for etf, start, end, *_ in stored] == [
        ("QQQ", date(2023, 4, 15), date(2024, 4, 15)), ("SPY", date(2023, 4, 15), date(2024, 4, 15)),
        ("QQQ", date(2023, 5, 6), date(2024, 5, 6)), ("SPY", date(2023, 5, 6), date(2024, 5, 6)),
    ]
    assert all(version == "v2" and 0 < n == labelled <= 200 for *_, version, n, labelled in stored)
    assert [counts.get("skipped") is not None for _, counts in runs] == [False, True, False, True]
    assert runs[0][1]["SPY"]["outcomes"] + runs[0][1]["SPY"]["unavailable"] == 200


def test_shares_are_derived_from_counts(db):
    seed_history(db)
    config = replace(TEST_CONFIG, targets=["QQQ"], sensitivity=[],
                     base_rates=replace(TEST_CONFIG.base_rates, placebo_n=200, history_years=1))
    run_daily_job(db, recorded_alpaca(), config, date(2024, 4, 15))
    with db.connect() as conn:
        rows = conn.execute(text("SELECT n, low, medium, high, share_low, share_medium, share_high FROM base_rates")).all()
    for n, low, medium, high, s_low, s_medium, s_high in rows:
        assert (s_low, s_medium, s_high) == pytest.approx((low / n, medium / n, high / n))
