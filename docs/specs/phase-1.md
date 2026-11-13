# Spec: Valuequake Phase 1 - Market data, Outcome engine, Base Rates, Article collection

Status: ready-for-agent (no issue tracker configured yet; publish after `/setup-matt-pocock-skills`)
Vocabulary: `GLOSSARY.md`. Decisions: `docs/adr/0001` to `0004`, `docs/project-brief.md` (Decisions section).

## Problem Statement

I want to know whether Claude Opus can predict how strongly news moves specific ETFs, better than a calendar-aware baseline. Before any Prediction can be judged, I need a trustworthy definition of what actually happened (the Outcome), an honest baseline to beat (Base Rates and the Calendar Baseline), and months of post-cutoff news to evaluate on. Calendar time is the bottleneck: every day news isn't being collected is a day of evaluation data lost. Today none of this exists, only design docs.

## Solution

A local Python service (`docker compose up`: app + Postgres with pgvector) that, from day one:

- backfills 5 years of 1-min SIP bars for the fixed ETF list plus sensitivity tickers, and keeps them current every session;
- collects every Alpaca News Article (live from now on, plus 5 years of history for future Analogs);
- collects daily VIX and 10y yield, and builds a calendar of Scheduled Releases;
- computes the Outcome for any (ETF, publish time) with one deterministic engine (beta-adjusted abnormal return, SAR, impact label for h=1/3/5 and diagnostics);
- computes Base Rates from placebo publish times through that same engine, split by session timing and by Scheduled Release in window (the Calendar Baseline);
- runs as a daily job that catches up on missed sessions when the laptop wakes.

I inspect everything in Jupyter notebooks that import the same package. No UI, no LLM calls in this phase.

## User Stories

1. As the researcher, I want to start the whole system with one `docker compose up`, so that I don't manage services by hand.
2. As the researcher, I want the fixed ETF list (SPY, QQQ, SMH, XLE, XLF, TLT, GLD, IWM) and sensitivity tickers (UUP, USO) defined in one config file, so that changing the universe is a config edit.
3. As the researcher, I want 5 years of 1-min SIP bars backfilled for every configured ticker, so that betas, residual volatility and Base Rates have enough history.
4. As the researcher, I want the backfill to be resumable after interruption, so that a laptop sleep or rate limit never forces a restart from zero.
5. As the researcher, I want the backfill to respect Alpaca rate limits, so that my account isn't throttled or blocked.
6. As the researcher, I want bars always requested with `feed=sip`, so that volume reflects the whole market, not one exchange.
7. As the researcher, I want split-adjusted prices stored alongside the raw close, so that returns are continuous across splits and I can still display real prices.
8. As the researcher, I want dividends stored and added back on ex-dates in return calculations, so that TLT's monthly distributions don't show up as fake negative moves.
9. As the researcher, I want the trading calendar (holidays, half days) taken from Alpaca's calendar, so that windows and "next session" are always right.
10. As the researcher, I want each new session's bars ingested by the daily job, so that the data stays current without my intervention.
11. As the researcher, I want the daily job to catch up on every missed session on startup, so that sleeping through the US close (03:30-04:30 WIB) loses nothing.
12. As the researcher, I want re-running the daily job for the same date to change nothing, so that catch-up and retries are always safe.
13. As the researcher, I want every Alpaca News Article stored with its id, source, headline, summary, body, symbols, and UTC publish and update times, so that Phase 2 can cluster, filter and score them.
14. As the researcher, I want Articles collected live from the first day the system runs, so that post-cutoff evaluation data starts accumulating immediately (ADR 0001).
15. As the researcher, I want 5 years of historical Articles backfilled, so that Phase 2 has an Analog pool.
16. As the researcher, I want an Article that Alpaca updates later to keep its original publish time, so that no Outcome window ever starts after the fact.
17. As the researcher, I want a verification notebook that measures daily Article volume, whether the free plan returns full bodies, and any coverage gaps (e.g. 2016), so that I know if Alpaca News is good enough before Phase 2.
18. As the researcher, I want daily VIX and 10y yield from FRED stored, so that sensitivity betas and the Phase 2 market snapshot have inputs Alpaca can't provide.
19. As the researcher, I want CPI, NFP, PCE and GDP release dates and times from FRED, so that I have a historical Scheduled Release calendar.
20. As the researcher, I want FOMC decision dates and times kept in config, so that the calendar includes the most important release that FRED doesn't list.
21. As the researcher, I want the Outcome for any (ETF, publish time, horizon) computed by one function, so that labels, Base Rates and later Analogs share one definition.
22. As the researcher, I want the event window to follow the timing rules exactly (pre-market/overnight/weekend from the prior close, during market hours from the last 1-min close before publish, after close from that day's close), so that the label measures the reaction to the news and not the drift before it.
23. As the researcher, I want the abnormal return to use a beta against SPY estimated from trading days [t-250, t-11] before the event, so that sector ETFs aren't credited with market-wide moves.
24. As the researcher, I want equity ETF betas shrunk toward 1 (0.7 OLS + 0.3) and TLT/GLD betas left unshrunk, so that noisy betas don't distort abnormal returns.
25. As the researcher, I want SPY's abnormal return to be its raw return, so that market-wide news still counts for SPY.
26. As the researcher, I want residual volatility from an EWMA (lambda 0.94) with a floor of half the long-run residual volatility, so that calm regimes don't inflate SAR.
27. As the researcher, I want SAR scaled by the square root of the window length, including partial first sessions, so that intraday Articles are comparable to overnight ones.
28. As the researcher, I want the impact label (low below 1, medium 1 to 2, high at or above 2 |SAR|) and the realized direction, so that Predictions have a ground truth.
29. As the researcher, I want h=3 and h=5 Outcomes plus a reversal flag, so that I can study persistence as a daily trader.
30. As the researcher, I want `pre_sar` (3 sessions before t0) and abnormal volume stored with every Outcome, so that priced-in news and absorbed news are visible.
31. As the researcher, I want each Outcome flagged `confounded` when a Scheduled Release falls inside its window, so that confounded rows can be sliced out.
32. As the researcher, I want every input to the Outcome (beta, sigma, Base Rates) computed only from data before t0, so that there is no lookahead.
33. As the researcher, I want each Outcome to carry a `label_version`, so that changing the definition never silently mixes old and new labels.
34. As the researcher, I want Base Rates computed from random placebo publish times per ETF, run through the same Outcome function, so that the baseline measures exactly what the labels measure (ADR 0002).
35. As the researcher, I want Base Rates split by session timing (pre-market, during market hours, after close, weekend), so that partial-day windows are compared fairly.
36. As the researcher, I want Base Rates also split by whether a Scheduled Release falls in the window, so that the Calendar Baseline exists before Phase 2 needs it.
37. As the researcher, I want Base Rates stored with the sample size, history window and `label_version`, so that I can judge their stability and reproduce them.
38. As the researcher, I want Base Rates refreshed monthly by the daily job, so that they track regime changes without manual work.
39. As the researcher, I want a Base Rate report notebook showing per-ETF low/medium/high shares and whether "high" exceeds 15%, so that I can decide on switching to per-ETF percentile thresholds before Phase 2.
40. As the researcher, I want an Outcome inspection notebook where I pick an ETF and a timestamp and see the window, prices, beta, sigma, SAR and label, so that I can sanity-check the engine against a chart.
41. As the researcher, I want job runs logged with what was ingested and what failed, so that silent gaps are visible.
42. As the researcher, I want a data-gap check that reports sessions with missing or partial bars per ticker, so that bad data doesn't quietly become bad labels.
43. As the researcher, I want API keys (Alpaca, FRED) read from environment variables, so that secrets never land in config files.

## Implementation Decisions

- **One Python package** imported by the app, the jobs and the notebooks (stack: pandas, numpy, scipy, statsmodels, SQLAlchemy or equivalent, APScheduler, Alpaca Python SDK, a FRED HTTP client). No LLM dependency in Phase 1.
- **Deployment:** `docker compose` with two services, the app (a long-running process hosting the APScheduler daily job, with catch-up on startup) and Postgres with pgvector enabled (vectors are unused until Phase 2). No SQLite phase (Q17).
- **Modules:**
  - *Config:* ETF list and roles (target vs sensitivity), FOMC dates/times, label thresholds, beta shrink rule, EWMA lambda, floor ratio, placebo sample size, `label_version`. One file, versioned.
  - *Market data:* Alpaca bars (1-min, `feed=sip`, split-adjusted + raw close), trading calendar, dividends (corporate actions). Idempotent upserts keyed on (ticker, timestamp). Backfill tracks progress per (ticker, month) so it can resume.
  - *News:* Alpaca News ingest, idempotent on the Alpaca Article id; an update replaces content but never `published_at`. No pre-filter in Phase 1: everything is stored (around 130 Articles a day).
  - *Macro:* FRED series (VIXCLS, DGS10) and release dates for CPI, NFP, PCE, GDP, merged with config FOMC dates into one Scheduled Release table (name, release time in UTC, source).
  - *Outcome engine (deep module, pure):* `outcome(etf, published_at, horizons, market_data_view) -> Outcome`. Internally: timing classification, window (t0, t1) per horizon, beta estimate, EWMA residual sigma with floor, AR, SAR, label, direction, `pre_sar`, abnormal volume, reversal flag, `confounded`. It reads only through a market-data view that refuses data at or after t0 for every estimated input, so lookahead is impossible by construction.
  - *Base Rates:* draw placebo publish times uniformly over real calendar minutes per ETF in the history window, run the Outcome engine, aggregate by (ETF, timing bucket, Scheduled-Release-in-window, `label_version`). Store counts, shares, n and window. Refreshed monthly.
  - *Daily job:* for each session missing since the last success, in order: bars → dividends/calendar → Articles → FRED → Scheduled Releases → (monthly) Base Rates. Each step is idempotent and logged as a job run row.
- **Returns are total returns:** split-adjusted bar prices with dividends added back on ex-dates. Returns use log returns for beta estimation and simple returns over the window for AR.
- **Schema (logical):** bars_1m, trading_sessions, dividends, articles, macro_series, scheduled_releases, base_rates, job_runs. Betas and residual sigma are computed on demand inside the engine (cheap at this scale), not stored, so there is no second source of truth.
- **Earnings as Scheduled Releases and ETF holdings move to Phase 2.** They're only needed for the pre-filter and the full Calendar Baseline. Phase 1's `confounded` covers CPI, NFP, PCE, GDP and FOMC.
- **Outcomes are not persisted in Phase 1.** An Outcome belongs to an Event, and Events (clustering) arrive in Phase 2. Phase 1 delivers the engine plus Base Rates.

## Testing Decisions

- Good tests check external behavior through the two agreed seams only. No mocks inside the package, no per-function unit suites.
- **Seam 1: Outcome engine (pure).** Hand-built 1-min bar fixtures for an ETF and SPY with known answers:
  - one fixture per timing case: pre-market, during market hours, after close, weekend, holiday-adjacent, half day;
  - h=1/3/5 window ends across a weekend;
  - beta shrink vs no shrink (equity vs TLT/GLD);
  - SPY raw-return rule;
  - EWMA recursion and the floor kicking in;
  - SAR scaling for a partial session;
  - label boundaries at exactly 1 and 2;
  - the dividend add-back on an ex-date;
  - `confounded` true and false;
  - lookahead guard (a bar at or after t0 changes nothing in beta or sigma).
  Base Rates are tested through the same seam: a fixture with known return distribution gives known shares.
- **Seam 2: daily job (integration).** Real Postgres in Docker. Alpaca bars, Alpaca News and FRED are replaced by recorded responses at the HTTP-client boundary. Tests:
  - running a date stores bars, Articles, macro series and Scheduled Releases;
  - running it twice changes nothing;
  - a three-session gap is caught up in order;
  - an Article update keeps `published_at`;
  - an interrupted backfill resumes where it stopped.
- No prior art in the repo (greenfield). Use `py-market-profile` only as later cross-check for Phase 3, not here.

## Out of Scope

- Any LLM call, Event clustering, embeddings, Predictions, Analogs, RPSS (Phase 2).
- Pre-filter, ETF holdings, earnings calendar, sensitivity-label generation (Phase 2).
- AMT engine, profiles, placebo-level tests (Phase 3), AMT A/B (Phase 4).
- Any UI (Streamlit starts in Phase 2). Watchlist toggle, alerts, intraday 1h horizon, self-consistency (cut from MVP).
- SUS / macro consensus data.

## Further Notes

- Free-plan risks to check early (notebook from story 17): whether Alpaca News returns full bodies, its real daily volume, the reported 2016 gap; whether FRED release-date history is complete for the four indicators.
- Expected storage: about 11M bar rows for 10 tickers over 5 years, well within local Postgres.
- Phase 1 is done when the Base Rate report exists for all 8 ETFs, the Outcome inspection notebook matches a manual check on a handful of known news days (e.g. a hot CPI print), and the daily job has run unattended for a week with catch-up exercised at least once.
