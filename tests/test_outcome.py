"""Outcome engine against hand-built bars with known answers (no database)."""

from dataclasses import replace
from datetime import date, datetime, time

import numpy as np
import pandas as pd
import pytest

from valuequake.config import load_config
from valuequake.market_data import LookaheadError, MarketView
from valuequake.outcome import impact_label, outcome

PARAMS = load_config("config.toml").outcome
NY = "America/New_York"
HOLIDAYS = {date(2024, 6, 19), date(2024, 7, 4)}
HALF_DAY = date(2024, 7, 3)
ANCHOR = date(2024, 6, 10)  # beta window is exact for events on this session
BETAS = {"QQQ": 1.5, "TLT": -0.3}


def et(d: date, hh: int, mm: int = 0) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, time(hh, mm)), tz=NY).tz_convert("UTC")


def calendar() -> pd.DataFrame:
    days = [d.date() for d in pd.bdate_range("2023-01-02", "2024-07-31") if d.date() not in HOLIDAYS]
    return pd.DataFrame({
        "date": days,
        "open_at": [et(d, 9, 30) for d in days],
        "close_at": [et(d, 13 if d == HALF_DAY else 16) for d in days],
    })


def daily_returns(sessions: pd.DataFrame, calm_last: int = 0) -> dict[str, np.ndarray]:
    """Daily log returns per ticker: r_i = beta_i * r_SPY + e_i, with e_i orthogonal to [1, r_SPY] over the
    anchor's beta window so OLS recovers beta_i exactly. `calm_last` shrinks e_i on the sessions just before
    the anchor to make the EWMA fall under the floor."""
    rng = np.random.default_rng(7)
    n = len(sessions)
    spy = rng.normal(0, 0.01, n)
    d = int(sessions.index[sessions["date"] == ANCHOR][0])
    window = slice(d - 250, d - 10)  # sessions D-250 .. D-11
    out = {"SPY": spy}
    for ticker, beta in BETAS.items():
        e = rng.normal(0, 0.008, n)
        basis = np.column_stack([np.ones(250 - 10), spy[window]])
        e[window] -= basis @ np.linalg.lstsq(basis, e[window], rcond=None)[0]
        if calm_last:
            e[d - calm_last : d] *= 0.01
        out[ticker] = beta * spy + e
    return out


def make_view(
    calm_last: int = 0, extra: dict[str, list[tuple]] | None = None, dividends=None, raw_ratio: dict | None = None,
    releases: list[pd.Timestamp] | None = None,
) -> MarketView:
    """One RTH close bar (close_at - 1 min, volume 1000) per session, plus extra (ts, close[, volume]) bars per
    ticker, replacing any at the same minute. `raw_ratio` sets close_raw = close * ratio (as before a split)."""
    sessions = calendar()
    bars = {}
    for ticker, r in daily_returns(sessions, calm_last).items():
        closes = 100 * np.exp(np.cumsum(r))
        rows = [(ts, c, 1000) for ts, c in zip(sessions["close_at"] - pd.Timedelta(minutes=1), closes)]
        rows += [(*row, 0)[:3] for row in (extra or {}).get(ticker, [])]
        df = pd.DataFrame(rows, columns=["ts", "close", "volume"]).set_index("ts")
        df = df[~df.index.duplicated(keep="last")]
        bars[ticker] = df.assign(close_raw=df["close"] * (raw_ratio or {}).get(ticker, 1.0))
    return MarketView(sessions, bars, dividends, releases)


VIEW = make_view(extra={
    t: [
        (et(date(2024, 6, 11), 10, 59), 50.0 + k),  # last bar before an 11:00 publish
        (et(date(2024, 6, 11), 16, 30), 999.0),  # after hours: must never be a session close
        (et(HALF_DAY, 10, 59), 60.0 + k),
    ]
    for k, t in enumerate(["SPY", "QQQ", "TLT"])
})


def close(ticker: str, d: date) -> float:
    return VIEW.daily_closes().at[d, ticker]


@pytest.mark.parametrize("published, timing, session, t0, t1, window_days", [
    (et(ANCHOR, 8), "pre_market", ANCHOR, et(date(2024, 6, 7), 16), et(ANCHOR, 16), 1.0),
    (et(date(2024, 6, 8), 12), "weekend", ANCHOR, et(date(2024, 6, 7), 16), et(ANCHOR, 16), 1.0),
    (et(date(2024, 6, 7), 18), "after_close", ANCHOR, et(date(2024, 6, 7), 16), et(ANCHOR, 16), 1.0),
    (et(date(2024, 6, 11), 9, 30), "rth", date(2024, 6, 11), et(date(2024, 6, 11), 9, 30), et(date(2024, 6, 11), 16), 1.0),
    (et(date(2024, 6, 11), 11), "rth", date(2024, 6, 11), et(date(2024, 6, 11), 11), et(date(2024, 6, 11), 16), 300 / 390),
    (et(date(2024, 6, 11), 16), "after_close", date(2024, 6, 12), et(date(2024, 6, 11), 16), et(date(2024, 6, 12), 16), 1.0),
    # Juneteenth: news on the holiday or the evening before waits for Thursday.
    (et(date(2024, 6, 19), 10), "weekend", date(2024, 6, 20), et(date(2024, 6, 18), 16), et(date(2024, 6, 20), 16), 1.0),
    (et(date(2024, 6, 18), 17), "after_close", date(2024, 6, 20), et(date(2024, 6, 18), 16), et(date(2024, 6, 20), 16), 1.0),
    # Half day closes at 13:00; the session after it skips July 4.
    (et(HALF_DAY, 11), "rth", HALF_DAY, et(HALF_DAY, 11), et(HALF_DAY, 13), 120 / 210),
    (et(HALF_DAY, 14), "after_close", date(2024, 7, 5), et(HALF_DAY, 13), et(date(2024, 7, 5), 16), 1.0),
])
def test_timing_and_window(published, timing, session, t0, t1, window_days):
    o = outcome("QQQ", published, VIEW, PARAMS)
    assert (o.timing, o.session, o.t0, o.h1.t1) == (timing, session, t0, t1)
    assert o.h1.window_days == pytest.approx(window_days)


def test_prior_close_window_prices():
    o = outcome("QQQ", et(ANCHOR, 8), VIEW, PARAMS)
    assert (o.price_t0, o.h1.price_t1) == (close("QQQ", date(2024, 6, 7)), close("QQQ", ANCHOR))


def test_rth_window_starts_at_last_bar_before_publish_and_ignores_after_hours():
    o = outcome("QQQ", et(date(2024, 6, 11), 11), VIEW, PARAMS)
    assert o.price_t0 == 51.0
    assert o.h1.price_t1 == close("QQQ", date(2024, 6, 11)) != 999.0


def test_half_day_window_ends_at_early_close():
    o = outcome("TLT", et(HALF_DAY, 11), VIEW, PARAMS)
    assert (o.price_t0, o.h1.price_t1) == (62.0, close("TLT", HALF_DAY))


def test_equity_beta_shrunk_toward_one():
    o = outcome("QQQ", et(ANCHOR, 8), VIEW, PARAMS)
    assert o.beta_ols == pytest.approx(1.5)
    assert o.beta == pytest.approx(0.7 * 1.5 + 0.3)
    assert o.h1.ar == pytest.approx(o.h1.ret - o.beta * o.h1.spy_ret)


def test_tlt_beta_not_shrunk():
    o = outcome("TLT", et(ANCHOR, 8), VIEW, PARAMS)
    assert o.beta_ols == pytest.approx(-0.3) and o.beta == o.beta_ols


def test_spy_abnormal_return_is_raw_return():
    o = outcome("SPY", et(ANCHOR, 8), VIEW, PARAMS)
    assert o.beta is None and o.h1.ar == o.h1.ret
    assert o.h1.ret == pytest.approx(close("SPY", ANCHOR) / close("SPY", date(2024, 6, 7)) - 1)


def test_ewma_recursion_on_known_residuals():
    o = outcome("TLT", et(ANCHOR, 8), VIEW, PARAMS)
    sessions = calendar()
    r = daily_returns(sessions)
    d = int(sessions.index[sessions["date"] == ANCHOR][0])
    residuals = (r["TLT"] - BETAS["TLT"] * r["SPY"])[d - 250 : d]  # sessions D-250 .. D-1
    sigma_long = residuals[:240].std(ddof=1)
    s2 = sigma_long**2
    for e in residuals:
        s2 = 0.94 * s2 + 0.06 * e**2
    assert o.sigma_long == pytest.approx(sigma_long)
    assert o.sigma_ewma == pytest.approx(np.sqrt(s2))
    assert o.sigma == max(o.sigma_ewma, 0.5 * o.sigma_long)


def test_sigma_floor_kicks_in_after_calm_stretch():
    o = outcome("TLT", et(ANCHOR, 8), make_view(calm_last=60), PARAMS)
    assert o.sigma_ewma < 0.5 * o.sigma_long
    assert o.sigma == 0.5 * o.sigma_long


def test_partial_session_sar_scaling():
    o = outcome("QQQ", et(date(2024, 6, 11), 11), VIEW, PARAMS)
    assert o.h1.sar == pytest.approx(o.h1.ar / (o.sigma * np.sqrt(300 / 390)))
    assert o.h1.label == impact_label(o.h1.sar, (1.0, 2.0))
    assert o.h1.direction == np.sign(o.h1.ar)


@pytest.mark.parametrize("sar, label", [
    (0.999, "low"), (1.0, "medium"), (-1.0, "medium"), (1.999, "medium"), (2.0, "high"), (-2.0, "high"), (0.0, "low"),
])
def test_label_boundaries(sar, label):
    assert impact_label(sar, PARAMS.label_thresholds) == label


def test_bars_at_or_after_t0_do_not_change_beta_or_sigma():
    published = et(date(2024, 6, 11), 11)
    # A bar at t0, one after it, and replaced close bars for D and a later session.
    later = [(published, 2.0), (published + pd.Timedelta(minutes=5), 2.5),
             (et(date(2024, 6, 11), 15, 59), 3.0, 1000), (et(date(2024, 6, 20), 15, 59), 4.0, 1000)]
    shifted = make_view(extra={t: later for t in ["SPY", "QQQ"]})
    base, moved = outcome("QQQ", published, make_view(), PARAMS), outcome("QQQ", published, shifted, PARAMS)
    assert moved.h1.price_t1 == 3.0 != base.h1.price_t1
    fields = ("beta_ols", "beta", "sigma_ewma", "sigma_long", "sigma")
    assert [getattr(moved, f) for f in fields] == [getattr(base, f) for f in fields]


def test_view_refuses_prices_after_cutoff():
    cutoff = et(date(2024, 6, 11), 11)
    view = VIEW.before(cutoff)
    assert view.last_price("QQQ", cutoff) == 51.0
    with pytest.raises(LookaheadError):
        view.last_price("QQQ", cutoff + pd.Timedelta(minutes=1))
    assert view.daily_closes().index[-1] == date(2024, 6, 10)


def test_unshrunk_list_comes_from_config():
    o = outcome("QQQ", et(ANCHOR, 8), VIEW, replace(PARAMS, unshrunk_beta=frozenset({"QQQ"})))
    assert o.beta == o.beta_ols


# Ticket 07: horizons, reversal, pre_sar, abnormal volume, dividends, label_version.
THU = date(2024, 6, 13)


def close_bar(ticker: str, d: date, price: float) -> tuple:
    return (et(d, 13 if d == HALF_DAY else 16) - pd.Timedelta(minutes=1), price, 1000)


def flat_spy(days: list[date], level: float) -> list[tuple]:
    """SPY close bars pinned to one level, so AR equals the ETF's own return over those sessions."""
    return [close_bar("SPY", d, level) for d in days]


@pytest.mark.parametrize("published, h3_end, h5_end", [
    (et(THU, 8), et(date(2024, 6, 17), 16), et(date(2024, 6, 20), 16)),  # weekend, then Juneteenth
    (et(date(2024, 7, 1), 8), et(HALF_DAY, 13), et(date(2024, 7, 8), 16)),  # half day, then July 4
])
def test_h3_h5_window_ends_skip_weekends_and_holidays(published, h3_end, h5_end):
    o = outcome("QQQ", published, VIEW, PARAMS)
    assert (o.h3.t1, o.h5.t1) == (h3_end, h5_end)
    assert (o.h3.window_days, o.h5.window_days) == (3.0, 5.0)
    assert o.h3.price_t1 == VIEW.daily_closes().at[h3_end.tz_convert(NY).date(), "QQQ"]


def test_longer_horizons_missing_until_their_sessions_close():
    o = outcome("QQQ", et(date(2024, 7, 30), 8), VIEW, PARAMS)  # the calendar ends on 07-31
    assert o.h1 is not None and o.h3 is None and o.h5 is None and o.reversal is None


@pytest.mark.parametrize("day1, day5, reversal", [
    (1.05, 0.97, True),  # big up move that is fully given back
    (1.05, 1.06, False),  # big up move that holds
    (1.001, 0.97, False),  # h=1 move below 1 SAR: a sign flip is not a reversal
])
def test_reversal_flag(day1, day5, reversal):
    base = VIEW.daily_closes()
    c, spy = base.at[date(2024, 6, 12), "QQQ"], base.at[date(2024, 6, 12), "SPY"]
    days = [THU, date(2024, 6, 14), date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 20)]
    view = make_view(extra={
        "SPY": flat_spy(days, spy),
        "QQQ": [close_bar("QQQ", THU, c * day1), close_bar("QQQ", date(2024, 6, 20), c * day5)],
    })
    o = outcome("QQQ", et(THU, 8), view, PARAMS)
    assert o.h1.ar == pytest.approx(day1 - 1) and o.h5.ar == pytest.approx(day5 - 1)
    assert o.reversal is reversal


def pre_drift_view(rth_bar: bool) -> MarketView:
    """SPY flat from the 06-07 close, QQQ up exactly 3% by the 06-12 close (or by 10:59 on 06-13)."""
    base = VIEW.daily_closes()
    c7, spy7 = base.at[date(2024, 6, 7), "QQQ"], base.at[date(2024, 6, 7), "SPY"]
    spy = flat_spy([date(2024, 6, 10), date(2024, 6, 11), date(2024, 6, 12)], spy7)
    qqq = [close_bar("QQQ", date(2024, 6, 12), c7 * 1.03)]
    if rth_bar:
        spy, qqq = spy + [(et(THU, 10, 59), spy7, 1000)], [(et(THU, 10, 59), c7 * 1.03, 1000)]
    return make_view(extra={"SPY": spy, "QQQ": qqq})


def test_pre_sar_over_three_sessions_before_prior_close():
    o = outcome("QQQ", et(THU, 8), pre_drift_view(rth_bar=False), PARAMS)
    assert o.pre_sar == pytest.approx(0.03 / (o.sigma * np.sqrt(3)))


def test_pre_sar_includes_elapsed_part_of_session_for_rth_news():
    o = outcome("QQQ", et(THU, 11), pre_drift_view(rth_bar=True), PARAMS)
    assert o.pre_sar == pytest.approx(0.03 / (o.sigma * np.sqrt(3 + 90 / 390)))


def test_abnormal_volume_vs_20_session_median():
    view = make_view(extra={"QQQ": [(et(THU, 12), 0.0, 9000), (et(THU, 17), 0.0, 50_000)]})  # after hours ignored
    view_closes = make_view().daily_closes()
    assert view.daily_closes().at[THU, "QQQ"] == view_closes.at[THU, "QQQ"]
    o = outcome("QQQ", et(THU, 8), view, PARAMS)
    assert o.abnormal_volume == pytest.approx(np.log(10))


def ex_dividend_view(dividends: pd.DataFrame | None, raw_ratio: dict | None = None) -> MarketView:
    """TLT drops by exactly its 0.50 distribution at the 06-13 ex-date open and is otherwise flat; SPY flat."""
    base = VIEW.daily_closes()
    spy, tlt = base.at[date(2024, 6, 12), "SPY"], base.at[date(2024, 6, 12), "TLT"] - 0.5
    return make_view(
        extra={
            "SPY": [*flat_spy([THU], spy), (et(THU, 10, 59), spy, 1000)],
            "TLT": [close_bar("TLT", THU, tlt), (et(THU, 10, 59), tlt, 1000)],
        },
        dividends=dividends, raw_ratio=raw_ratio,
    )


def test_ex_dividend_drop_is_not_a_negative_abnormal_return():
    without = outcome("TLT", et(THU, 8), ex_dividend_view(None), PARAMS)
    assert without.h1.ar < 0  # the control: the drop alone looks like a negative move
    dividend = pd.DataFrame({"ticker": ["TLT"], "ex_date": [THU], "amount": [0.5]})
    o = outcome("TLT", et(THU, 8), ex_dividend_view(dividend), PARAMS)
    assert o.h1.ar == pytest.approx(0, abs=1e-12)


def test_dividend_scaled_to_split_adjusted_prices():
    # Raw prices are twice the adjusted ones (a later 2:1 split), so a raw 1.00 is 0.50 in adjusted terms.
    dividend = pd.DataFrame({"ticker": ["TLT"], "ex_date": [THU], "amount": [1.0]})
    o = outcome("TLT", et(THU, 8), ex_dividend_view(dividend, raw_ratio={"TLT": 2.0}), PARAMS)
    assert o.h1.ar == pytest.approx(0, abs=1e-12)


def test_rth_news_on_ex_date_leaves_the_dividend_in_the_pre_event_window():
    # The price dropped at the open, before the news: the event window is flat, the pre-event drift carries it.
    dividend = pd.DataFrame({"ticker": ["TLT"], "ex_date": [THU], "amount": [0.5]})
    o = outcome("TLT", et(THU, 11), ex_dividend_view(dividend), PARAMS)
    no_div = outcome("TLT", et(THU, 11), ex_dividend_view(None), PARAMS)
    assert o.h1.ar == pytest.approx(0, abs=1e-12) and no_div.h1.ar == pytest.approx(0, abs=1e-12)
    start = VIEW.daily_closes().at[date(2024, 6, 7), "TLT"]  # pre window starts at the close 3 sessions before
    assert o.pre_sar - no_div.pre_sar == pytest.approx(0.5 / start / (o.sigma * np.sqrt(3 + 90 / 390)))


def test_outcome_carries_config_label_version():
    assert outcome("QQQ", et(THU, 8), VIEW, PARAMS).label_version == PARAMS.label_version == "v2"


# Ticket 08: confounded.
@pytest.mark.parametrize("published, release, confounded", [
    (et(THU, 8), et(THU, 8, 30), True),  # pre-market news, CPI at 08:30 before the open
    (et(THU, 8), et(date(2024, 6, 12), 16), True),  # exactly at t0 (the prior close)
    (et(THU, 8), et(THU, 16), True),  # exactly at t1
    (et(THU, 8), et(date(2024, 6, 12), 15, 59), False),  # a minute before t0
    (et(THU, 8), et(THU, 16, 1), False),  # a minute after t1
    (et(date(2024, 6, 12), 14, 30), et(date(2024, 6, 12), 14), False),  # news after the FOMC decision
])
def test_confounded_when_a_release_is_inside_the_window(published, release, confounded):
    o = outcome("QQQ", published, make_view(releases=[release]), PARAMS)
    assert o.h1.confounded is confounded


def test_longer_horizon_can_be_confounded_when_h1_is_not():
    o = outcome("QQQ", et(THU, 8), make_view(releases=[et(date(2024, 6, 17), 8, 30)]), PARAMS)
    assert (o.h1.confounded, o.h3.confounded, o.h5.confounded) == (False, True, True)
