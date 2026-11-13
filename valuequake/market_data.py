import copy
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import Connection, Engine, text

ONE_MIN = pd.Timedelta(minutes=1)


class LookaheadError(Exception):
    pass


class MarketView:
    """Read-only market data for the Outcome engine.

    `sessions` has columns date, open_at, close_at (UTC); `bars` maps ticker to 1-min bars indexed by bar
    start (UTC) with `close` and optional `close_raw` and `volume` columns; `dividends` has columns ticker,
    ex_date, amount (raw per-share); `releases` holds Scheduled Release times (UTC).
    A bar counts as known once it has ended (ts + 1 min).
    `before(t)` returns a view that refuses or hides everything not known by t.
    """

    def __init__(
        self,
        sessions: pd.DataFrame,
        bars: dict[str, pd.DataFrame],
        dividends: pd.DataFrame | None = None,
        releases: list[pd.Timestamp] | None = None,
    ):
        self.sessions = sessions.sort_values("date").reset_index(drop=True)
        self._bars = {t: b.sort_index() for t, b in bars.items()}
        per_ticker = {t: _session_stats(self.sessions, b) for t, b in self._bars.items()}
        index = self.sessions["date"]
        self._closes = pd.DataFrame({t: s["close"] for t, s in per_ticker.items()}, index=index)
        self._volumes = pd.DataFrame({t: s["volume"] for t, s in per_ticker.items()}, index=index)
        raw_closes = pd.DataFrame({t: s["close_raw"] for t, s in per_ticker.items()}, index=index)
        self._dividends = _adjusted_dividends(self._closes, raw_closes, dividends)
        self._releases = pd.DatetimeIndex(sorted(releases or []), tz="UTC")
        self.cutoff: pd.Timestamp | None = None

    def before(self, t: pd.Timestamp) -> "MarketView":
        view = copy.copy(self)
        view.cutoff = t
        return view

    def _closed(self, frame: pd.DataFrame) -> pd.DataFrame:
        if self.cutoff is None:
            return frame
        return frame[(self.sessions["close_at"] <= self.cutoff).to_numpy()]

    def daily_closes(self) -> pd.DataFrame:
        """RTH close per session (rows) and ticker (columns), for sessions closed by the cutoff."""
        return self._closed(self._closes)

    def daily_volumes(self) -> pd.DataFrame:
        """RTH volume per session and ticker, for sessions closed by the cutoff."""
        return self._closed(self._volumes)

    def dividends(self) -> pd.DataFrame:
        """Dividend per share in split-adjusted price terms, on its ex-date session (0 elsewhere).

        Not cut off: dividends are announced before their ex-date, so they are known in advance.
        """
        return self._dividends

    def release_between(self, start: pd.Timestamp, end: pd.Timestamp) -> bool:
        """Whether a Scheduled Release falls inside [start, end]. Not cut off: releases are scheduled in advance."""
        return bool(self._releases.searchsorted(start) < self._releases.searchsorted(end, side="right"))

    def last_price(self, ticker: str, t: pd.Timestamp) -> float:
        """Close of the last bar that ended at or before t."""
        if self.cutoff is not None and t > self.cutoff:
            raise LookaheadError(f"{ticker} price at {t} is after the view cutoff {self.cutoff}")
        bars = self._bars[ticker]
        pos = bars.index.searchsorted(t - ONE_MIN, side="right") - 1
        return float(bars["close"].iloc[pos]) if pos >= 0 else np.nan


def _session_stats(sessions: pd.DataFrame, bars: pd.DataFrame) -> dict[str, np.ndarray]:
    """Per session: close and raw close of the last bar starting inside [open_at, close_at), and RTH volume."""
    if bars.empty:
        missing = np.full(len(sessions), np.nan)
        return {"close": missing, "close_raw": missing, "volume": missing}
    open_at, close_at = pd.DatetimeIndex(sessions["open_at"]), pd.DatetimeIndex(sessions["close_at"])
    last = bars.index.searchsorted(close_at - ONE_MIN, side="right") - 1
    safe = last.clip(0)
    has_bar = (last >= 0) & (bars.index[safe] >= open_at)

    def at_close(column: str) -> np.ndarray:
        return np.where(has_bar, bars[column].to_numpy(dtype=float)[safe], np.nan)

    session = open_at.searchsorted(bars.index, side="right") - 1
    in_rth = (session >= 0) & (bars.index < close_at[session.clip(0)])
    volume = bars["volume"].to_numpy(dtype=float) if "volume" in bars else np.zeros(len(bars))
    volumes = np.bincount(session[in_rth], weights=volume[in_rth], minlength=len(sessions))
    return {
        "close": at_close("close"),
        "close_raw": at_close("close_raw") if "close_raw" in bars else at_close("close"),
        "volume": np.where(has_bar, volumes, np.nan),  # a session without bars is a gap, not zero volume
    }


def _adjusted_dividends(closes: pd.DataFrame, raw_closes: pd.DataFrame, dividends: pd.DataFrame | None) -> pd.DataFrame:
    """Raw dividends scaled by the prior session's adjusted/raw close, so they match split-adjusted prices."""
    out = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    if dividends is None or dividends.empty:
        return out
    split_ratio = (closes / raw_closes).shift(1).fillna(1.0)
    per_session = dividends.groupby(["ex_date", "ticker"])["amount"].sum().unstack()
    per_session = per_session.reindex(index=closes.index, columns=closes.columns).fillna(0.0)
    return out + per_session * split_ratio


def load_view(engine: Engine | Connection, tickers: list[str], start: date, end: date) -> MarketView:
    """Calendar, 1-min bars, dividends and Scheduled Releases between start and end (session dates, inclusive)."""
    params = {"start": start, "end": end, "tickers": tickers}
    sessions = pd.read_sql(
        text("SELECT date, open_at, close_at FROM trading_sessions WHERE date BETWEEN :start AND :end"),
        engine, params=params,
    )
    bars = pd.read_sql(
        text("""
            SELECT ticker, ts, close, close_raw, volume FROM bars_1m
            WHERE ticker = ANY(:tickers) AND ts >= :start AND ts < CAST(:end AS date) + 2
            ORDER BY ticker, ts
        """),
        engine, params=params, index_col="ts",
    )
    dividends = pd.read_sql(
        text("SELECT ticker, ex_date, amount FROM dividends WHERE ticker = ANY(:tickers) AND ex_date BETWEEN :start AND :end"),
        engine, params=params,
    )
    releases = pd.read_sql(
        text("SELECT released_at FROM scheduled_releases WHERE released_at >= :start AND released_at < CAST(:end AS date) + 2"),
        engine, params=params,
    )
    sessions[["open_at", "close_at"]] = sessions[["open_at", "close_at"]].apply(pd.to_datetime, utc=True)
    bars.index = pd.to_datetime(bars.index, utc=True)
    by_ticker = {t: b.drop(columns="ticker") for t, b in bars.groupby("ticker")}
    # A ticker without any bars yet still gets a (NaN) column, so Outcomes report it as unavailable.
    empty = bars.drop(columns="ticker").iloc[:0]
    return MarketView(
        sessions, {t: by_ticker.get(t, empty) for t in tickers}, dividends,
        list(pd.to_datetime(releases["released_at"], utc=True)),
    )
