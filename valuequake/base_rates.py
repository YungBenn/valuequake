"""Base Rates from placebo publish times run through the Outcome engine (ADR 0002)."""

import zlib
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Connection, text

from valuequake.alpaca import NY
from valuequake.config import Config, OutcomeParams
from valuequake.market_data import MarketView, load_view
from valuequake.outcome import BENCHMARK, Outcome, OutcomeUnavailable, outcome

LABELS = ["low", "medium", "high"]
# Enough sessions before the window for the beta window (250 sessions is about 365 calendar days).
HISTORY_MARGIN = timedelta(days=400)


def placebo_times(start: pd.Timestamp, end: pd.Timestamp, n: int, rng: np.random.Generator) -> pd.DatetimeIndex:
    """n publish times, uniform to the second over [start, end). Nights, weekends and holidays are included, so
    every timing bucket gets draws in proportion to its share of calendar time."""
    seconds = np.sort(rng.integers(0, int((end - start).total_seconds()), n))
    return start + pd.to_timedelta(seconds, unit="s")


def placebo_outcomes(etf: str, view: MarketView, params: OutcomeParams, times) -> tuple[list[Outcome], int]:
    """Outcomes for the placebo times, plus how many had too little history or missing prices."""
    outcomes, unavailable = [], 0
    for p in times:
        try:
            outcomes.append(outcome(etf, p, view, params))
        except OutcomeUnavailable:
            unavailable += 1
    return outcomes, unavailable


def aggregate(outcomes: list[Outcome]) -> pd.DataFrame:
    """h=1 label counts per (timing, release_in_window), with n."""
    columns = ["timing", "release_in_window", *LABELS, "n"]
    if not outcomes:
        return pd.DataFrame(columns=columns)
    labels = pd.DataFrame({
        "timing": [o.timing for o in outcomes],
        "release_in_window": [o.h1.confounded for o in outcomes],
        "label": [o.h1.label for o in outcomes],
    })
    counts = labels.value_counts().unstack("label", fill_value=0).reindex(columns=LABELS, fill_value=0)
    counts["n"] = counts.sum(axis=1)
    return counts.reset_index()[columns]


UPSERT_BASE_RATE = text("""
    INSERT INTO base_rates (etf, timing, release_in_window, label_version, window_start, window_end, seed, n, low, medium, high)
    VALUES (:etf, :timing, :release_in_window, :label_version, :window_start, :window_end, :seed, :n, :low, :medium, :high)
    ON CONFLICT (etf, timing, release_in_window, label_version, window_end) DO UPDATE SET
        window_start = EXCLUDED.window_start, seed = EXCLUDED.seed, n = EXCLUDED.n,
        low = EXCLUDED.low, medium = EXCLUDED.medium, high = EXCLUDED.high, computed_at = now()
""")


def refresh_base_rates(conn: Connection, config: Config, window_end: date) -> dict:
    """Draw placebo times over the history window ending at window_end's close and store Base Rates per ETF."""
    b, params = config.base_rates, config.outcome
    window_start = (pd.Timestamp(window_end) - pd.DateOffset(years=b.history_years)).date()
    end_close = conn.execute(
        text("SELECT close_at FROM trading_sessions WHERE date <= :d ORDER BY date DESC LIMIT 1"), {"d": window_end}
    ).scalar()
    start = pd.Timestamp(datetime.combine(window_start, time(), NY))
    counts = {}
    for etf in config.targets:
        view = load_view(conn, sorted({etf, BENCHMARK}), window_start - HISTORY_MARGIN, window_end)
        rng = np.random.default_rng([b.seed, zlib.crc32(etf.encode())])
        outcomes, unavailable = placebo_outcomes(etf, view, params, placebo_times(start, pd.Timestamp(end_close), b.placebo_n, rng))
        rows = aggregate(outcomes).assign(
            etf=etf, label_version=params.label_version, window_start=window_start, window_end=window_end, seed=b.seed,
        )
        if len(rows):
            conn.execute(UPSERT_BASE_RATE, rows.to_dict("records"))
        counts[etf] = {"outcomes": len(outcomes), "unavailable": unavailable}
    return counts
