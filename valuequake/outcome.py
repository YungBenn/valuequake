"""The Outcome engine: realized impact of an (ETF, publish time), docs/etf-impact-scoring.md Part A."""

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from valuequake.config import OutcomeParams
from valuequake.market_data import MarketView

BENCHMARK = "SPY"
NY = "America/New_York"
PRE_SESSIONS = 3  # pre_sar looks back this many sessions before t0
VOLUME_SESSIONS = 20  # abnormal volume compares D to the median of this many sessions before it


class OutcomeUnavailable(Exception):
    """Not enough history before t0, or the h=1 window has not closed yet."""


@dataclass(frozen=True)
class Window:
    """t0 to the close of D + h - 1. Returns are total returns: dividends are added back on ex-dates."""

    horizon: int
    t1: pd.Timestamp
    price_t1: float
    ret: float
    spy_ret: float
    window_days: float  # T = f + h - 1
    ar: float
    sar: float
    label: str
    direction: int  # sign of AR
    confounded: bool  # a Scheduled Release falls inside [t0, t1]


@dataclass(frozen=True)
class Outcome:
    etf: str
    published_at: pd.Timestamp
    timing: str  # pre_market | rth | after_close | weekend (any non-session day, holidays included)
    session: date  # D: the first session whose close is after published_at
    t0: pd.Timestamp
    price_t0: float
    beta_ols: float | None  # None for SPY, whose expected return is 0
    beta: float | None
    sigma_ewma: float
    sigma_long: float
    sigma: float
    h1: Window
    h3: Window | None  # None until that window has closed
    h5: Window | None
    reversal: bool | None  # SAR sign flips from h=1 to h=5 after a move of at least 1
    pre_sar: float  # SAR over the 3 sessions before t0, up to t0
    abnormal_volume: float  # log(RTH volume of D / median of the 20 sessions before D)
    label_version: str


def impact_label(sar: float, thresholds: tuple[float, float]) -> str:
    medium, high = thresholds
    return "high" if abs(sar) >= high else "medium" if abs(sar) >= medium else "low"


def outcome(etf: str, published_at: pd.Timestamp, view: MarketView, params: OutcomeParams) -> Outcome:
    p = pd.Timestamp(published_at).tz_convert("UTC")
    sessions = view.sessions
    i = int(sessions["close_at"].searchsorted(p, side="right"))
    if i == 0 or i >= len(sessions):
        raise OutcomeUnavailable(f"calendar does not cover {p}")
    prev, d = sessions.iloc[i - 1], sessions.iloc[i]

    closes, dividends = view.daily_closes(), view.dividends()
    if p >= d.open_at:
        timing, t0 = "rth", p
        # Share of D's session left after publish; half days count as a full session.
        f = (d.close_at - p) / (d.close_at - d.open_at)
        p0, spy0 = view.last_price(etf, p), view.last_price(BENCHMARK, p)
    else:
        local = p.tz_convert(NY).date()
        timing = "after_close" if local == prev.date else "pre_market" if local == d.date else "weekend"
        t0, f = prev.close_at, 1.0
        p0, spy0 = closes.at[prev.date, etf], closes.at[prev.date, BENCHMARK]
    if np.isnan([p0, spy0]).any():
        raise OutcomeUnavailable(f"missing {etf} or {BENCHMARK} price at {t0}")

    estimation = view.before(t0)
    beta_ols, beta, sigma_ewma, sigma_long = _estimate(etf, estimation, params)
    sigma = max(sigma_ewma, params.sigma_floor_ratio * sigma_long)

    def total_return(ticker: str, start_price: float, start: pd.Timestamp, end_price: float, end: pd.Timestamp) -> float:
        # A dividend belongs to the window when its ex-date session opens inside (start, end].
        opens = sessions["open_at"]
        inside = ((opens > start) & (opens <= end)).to_numpy()
        return (end_price + dividends[ticker].to_numpy()[inside].sum()) / start_price - 1

    def abnormal(ret: float, spy_ret: float) -> float:
        return ret if beta is None else ret - beta * spy_ret

    def window(h: int) -> Window | None:
        if i + h - 1 >= len(sessions):
            return None
        end = sessions.iloc[i + h - 1]
        p1, spy1 = closes.at[end.date, etf], closes.at[end.date, BENCHMARK]
        if np.isnan([p1, spy1]).any():
            return None
        ret = total_return(etf, p0, t0, p1, end.close_at)
        spy_ret = total_return(BENCHMARK, spy0, t0, spy1, end.close_at)
        ar = abnormal(ret, spy_ret)
        window_days = float(f + h - 1)
        sar = float(ar / (sigma * np.sqrt(window_days)))
        return Window(
            horizon=h, t1=end.close_at, price_t1=float(p1), ret=float(ret), spy_ret=float(spy_ret),
            window_days=window_days, ar=float(ar), sar=sar,
            label=impact_label(sar, params.label_thresholds), direction=int(np.sign(ar)),
            confounded=view.release_between(t0, end.close_at),
        )

    h1, h3, h5 = window(1), window(3), window(5)
    if h1 is None:
        raise OutcomeUnavailable(f"{etf} h=1 window for {p} has not closed")

    # Pre-event drift from the close PRE_SESSIONS sessions before t0, read only from data known by t0.
    before = estimation.daily_closes()
    start = sessions.iloc[len(before) - PRE_SESSIONS - 1]
    pre_ar = abnormal(
        total_return(etf, before[etf].iloc[-PRE_SESSIONS - 1], start.close_at, p0, t0),
        total_return(BENCHMARK, before[BENCHMARK].iloc[-PRE_SESSIONS - 1], start.close_at, spy0, t0),
    )
    pre_sar = pre_ar / (sigma * np.sqrt(PRE_SESSIONS + 1 - f))

    volumes = view.daily_volumes()[etf].to_numpy()
    abnormal_volume = np.log(volumes[i] / np.nanmedian(volumes[i - VOLUME_SESSIONS : i]))

    return Outcome(
        etf=etf, published_at=p, timing=timing, session=d.date, t0=t0, price_t0=float(p0),
        beta_ols=beta_ols, beta=beta, sigma_ewma=sigma_ewma, sigma_long=sigma_long, sigma=sigma,
        h1=h1, h3=h3, h5=h5,
        reversal=None if h5 is None else bool(np.sign(h5.sar) != np.sign(h1.sar) and abs(h1.sar) >= 1),
        pre_sar=float(pre_sar), abnormal_volume=float(abnormal_volume), label_version=params.label_version,
    )


# ponytail: fixed minimum of usable daily returns in the beta window; tune once gap reports show real coverage.
MIN_RETURNS = 200


def _estimate(etf: str, view: MarketView, params: OutcomeParams) -> tuple[float | None, float | None, float, float]:
    """Beta and residual sigmas from daily total log returns of sessions closed before t0 (last row is D-1)."""
    first, last = params.beta_window
    tickers = [etf, BENCHMARK]
    closes = view.daily_closes()[tickers]
    dividends = view.dividends().loc[closes.index, tickers]
    returns = np.log((closes + dividends) / closes.shift(1)).to_numpy()
    if len(returns) < first + 1:
        raise OutcomeUnavailable(f"{etf}: {len(returns)} sessions before t0, need {first + 1}")
    recent = returns[-first:]  # sessions D-250 .. D-1
    window = recent[: first - last + 1]  # sessions D-250 .. D-11
    y, x = window[:, 0], window[:, 1]
    ok = ~np.isnan(y) & ~np.isnan(x)
    if ok.sum() < MIN_RETURNS:
        raise OutcomeUnavailable(f"{etf}: {ok.sum()} usable daily returns in the beta window, need {MIN_RETURNS}")

    if etf == BENCHMARK:
        beta_ols = beta = None
        residuals = recent[:, 1]
    else:
        beta_ols = float(np.cov(x[ok], y[ok])[0, 1] / np.var(x[ok], ddof=1))
        beta = beta_ols if etf in params.unshrunk_beta else params.beta_shrink * beta_ols + (1 - params.beta_shrink)
        residuals = recent[:, 0] - beta * recent[:, 1]

    sigma_long = float(np.nanstd(residuals[: first - last + 1], ddof=1))
    s2 = sigma_long**2
    lam = params.ewma_lambda
    for e in residuals[~np.isnan(residuals)]:
        s2 = lam * s2 + (1 - lam) * e**2
    return beta_ols, beta, float(np.sqrt(s2)), sigma_long
