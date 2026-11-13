# Project Brief: Valuequake

Valuequake = value (AMT value area) + quake (news shock). An ETF news impact analyzer with Auction Market Theory context.

Seed doc for `/grill-with-docs`. Challenge everything here before `/to-spec`.

## Problem
Lots of news hits markets every day. Most is noise. I want a web app that reads news, figures out which ETFs it touches, and estimates the probability that it will move each ETF a lot (high impact) or barely (low impact).

This is an analytics/learning tool, not a trading bot. No order execution.

Personal tool, runs locally, single user. Not going to production. Main goal: get answers fast (does Opus beat base rates? do AMT heuristics beat placebo?).

## Core idea
- Single LLM: Claude Opus 5.5 (`claude-opus-5-5`) does all scoring. No custom ML training.
- The model gets smarter through retrieval of similar past news + what actually happened to the ETF after (RAG on outcomes), not through training.
- Every prediction is later checked against the real price move so accuracy is measurable.

## MVP scope
- ETF universe (fixed, 5 to 10): SPY, QQQ, SMH, XLE, XLF, TLT, GLD, IWM
- News types: macro (CPI, jobs, FOMC, Fed speakers), top holdings company news, sector regulation, geopolitics/commodities
- Horizon: 1 trading day after publish (maybe add intraday 1h later)
- Output per (article, ETF): impact level, probability, direction, reasoning
- Auction Market Theory (AMT) technical analysis per ETF: volume profile, value area, balance/imbalance, key reference levels. Used as a standalone TA panel AND as context for news scoring. Full spec in `amt-module.md`.

## ETF watchlist toggle
User picks which ETFs the system works on (e.g. turn on VGT, VOO, SOXX only). Any US-listed ETF available on Alpaca can be added; the original 8 are just defaults.

Three states per ETF:
| State | Price data + AMT + outcomes (code, no LLM) | Opus news scoring + daily auction read |
|---|---|---|
| `off` | no | no |
| `data_only` | yes | no |
| `ai_on` | yes | yes |

Rules:
- `SPY` is always at least `data_only`: it is the benchmark for beta and abnormal returns, even if the user never turns it on.
- Turning an ETF on triggers a warm-up: backfill 1-min bars (250+ sessions for beta, EWMA, base rates, composites), compute AMT history, base rates, beta. Status shown as `warming_up` until ready; AI scoring starts only after warm-up.
- ETF metadata needed per ETF (top holdings + weights, sector, sensitivities) for pre-filter and prompt. Refresh monthly (and on enable).
  - Holdings source: `HoldingsProvider` interface with fallbacks: FMP API (`/stable/etf/holdings`, sector + country weightings, fund info) → Alpha Vantage `ETF_PROFILE` (holdings + sector allocation) → yfinance `funds_data` (top holdings only, unofficial) → manual CSV import (issuer download, e.g. iShares/Vanguard daily holdings) as override.
  - Store top holdings covering ~80% of weight or top 50, whichever is smaller (VGT has 300+ holdings; the tail is noise for the pre-filter).
  - Always store `holdings_as_of` date and source; warn in UI if older than 45 days.
  - Sensitivities are computed in code, not fetched: rolling 250d betas of the ETF's daily returns vs rates (TLT), USD (UUP), oil (USO), gold (GLD), volatility (VIX change). Turn into labels (e.g. |beta| above a threshold = "rate sensitive").
- Pre-filter only matches against holdings/keywords of `ai_on` ETFs, so irrelevant articles never reach Opus.
- Prompt `candidate_etfs` contains only `ai_on` ETFs that passed the pre-filter for that article.
- Overlap warning in the UI when two enabled ETFs are highly correlated (e.g. VOO vs SPY, SOXX vs SMH, correlation > 0.95 over 60d): same signal, double tokens.
- Optional cap on number of `ai_on` ETFs + estimated monthly token cost shown on the settings page.
- Toggle changes apply from the next job run. Turning off keeps all history; turning back on resumes without re-backfilling.
- Eval and calibration: per-ETF metrics only count periods when the ETF was `ai_on`. New ETFs use pooled calibration until they have enough labeled clusters.

## Pipeline
1. Ingest: poll news API(s) + RSS. Store raw article with accurate publish timestamp (UTC).
2. Dedupe: cluster near-duplicate articles, keep earliest, flag rehashes.
3. Pre-filter (cheap, no LLM): entity/keyword match against ETF holdings + macro keyword list. Drop irrelevant.
4. Context build: ETF profile (top holdings + weights, sector, rate sensitivity), market snapshot (ETF 1d/5d return, 20d realized vol, VIX, upcoming scheduled events), AMT state snapshot at publish time (computed in code, see `amt-module.md`), top-k similar past events with realized outcomes.
5. Score: call Opus 5.5 with the scoring prompt (`prompts/etf-impact-scoring.md`), JSON output, validate against schema, retry once on invalid.
6. Store prediction.
7. Outcome job: after horizon passes, compute beta-adjusted abnormal return and SAR (plus h3/h5, abnormal volume, confounded flag), store as outcome, link to prediction. Outcomes feed back into RAG.
8. Evaluate: weekly report (RPS, RPSS vs base rates, log loss, reliability per class, direction hit rate). A/B: scoring with vs without AMT context. Post-hoc calibration once ~300 to 500 labeled clusters exist.

AMT pipeline (parallel):
- Ingest 1-min intraday bars per ETF
- AMT engine (pure code): TPO + volume profiles (RTH only, ATR-based rows, closing auction excluded), IB, rotation factor, day type, value migration, daily/weekly one-timeframing, bracket detection (breakout/accepted/failed), bracket + 20d + 60d composites, HVN/LVN, naked POCs
- AMT heuristics validated against placebo levels before use (see `amt-module.md` Section 9)
- Store AMT state per session + intraday snapshots
- After close: Opus 5.5 writes daily auction read per ETF (AMT narrative prompt in `amt-module.md`)

## Impact labels (ground truth definition, v2)
Full spec in `etf-impact-scoring.md` Part A. Summary:
- Abnormal return = ETF return minus beta x SPY return (market model, beta from 250d window; SPY uses raw return)
- Window starts at last price before publish (prior close for overnight/pre-market news), ends at close of next session (h=1); h=3 and h=5 stored too
- SAR = abnormal return / (EWMA residual vol, lambda 0.94, with long-run floor, scaled by sqrt of window length)
- high: |SAR| >= 2, medium: 1 <= |SAR| < 2, low: |SAR| < 1 (switch to per-ETF percentiles if base rates are skewed)
- Evaluate per event cluster, not per article. Primary metric: RPS / RPSS vs base rates.
- Must be the same definition the prompt uses.

## Web app features
- News feed sorted by time, impact badge per ETF
- ETF page: recent scored news, price chart with news markers, prediction accuracy for this ETF
- Article detail: reasoning, similar past events used, actual outcome once available
- Accuracy dashboard
- Alerts (later): notify when high impact prob > threshold

## Stack (DECIDED: Python only)
Why: the project is mostly a research loop. One language means the daily job and the backtests call the exact same functions, so formulas (SAR, profiles, value area) can never drift between "app" and "research".

- Language: Python only (one package, `valuequake/`, imported by app, jobs and notebooks)
- Core libs: pandas, numpy, scipy, statsmodels (beta OLS), scikit-learn (calibration), `py-market-profile` as reference/cross-check for the AMT engine
- API: FastAPI
- Scheduler: APScheduler inside the app. Daily AMT + outcome jobs, plus catch-up on startup (process any missed sessions), because the laptop may be asleep at US close (~03:30 to 04:30 WIB)
- DB: Postgres + pgvector (similar-event retrieval). No TimescaleDB needed at this scale. SQLite acceptable for a first spike.
- Queue: none. Postgres table for pending work if needed.
- LLM: Anthropic Python SDK with an Anthropic API key, called directly (no router or proxy), model pinned to `claude-opus-5-5` with no model fallback. Structured outputs (`output_config.format`) for the scoring JSON, prompt caching on the static system prompt + ETF profiles. Each Prediction stores the model id and token usage the response reports.
- Data: Alpaca Python SDK (`feed=sip`)
- Embeddings: TBD provider
- Frontend: Streamlit + Plotly for MVP (fastest path to charts, profiles, tables). Move to Next.js later only if the UI becomes the bottleneck.
- Research: Jupyter notebooks importing the same package
- Run: `docker compose up` (app + Postgres). No k8s, no auth.

## Known risks
- Priced-in: by publish time market may already have moved. Timestamp accuracy is critical.
- Lookahead bias: retrieval and outcomes must only use data available before the article's publish time.
- LLM probabilities not calibrated by default; calibration report needed, maybe a post-hoc calibration map later.
- Cost: Opus per Event adds up (estimate $0.08-0.10 per Event, $60-200/month incl. paired A/Bs; measure in week 1 of Phase 2). Pre-filter and dedupe are mandatory.
- News API licensing/history depth.

## Decisions (grilling 2026-10-28)
Terms: see `GLOSSARY.md`. ADRs: `docs/adr/`.

Phases:
1. Bars (5y 1-min, SIP) + label engine + placebo Base Rates + live Article ingest from day one. Notebooks only, no UI.
2. Opus scoring of Events + RPSS + Streamlit (news feed, Prediction vs Outcome).
3. AMT engine + placebo tests + AMT panels.
4. AMT on/off A/B.

Evaluation:
- Only Articles published after Opus 5.5's training cutoff count toward RPSS and calibration (ADR 0001). Older history feeds code-computed values only.
- Base Rates come from placebo publish times with the same window rule, bucketed by session timing. Go/no-go is RPSS vs the Calendar Baseline (ADR 0002).
- All A/B tests are paired on the same Events (ADR 0003).

Scoring:
- One Prediction per (Event, ETF), built from the Event's earliest Article. Supersedes "one call per article".
- Scoring is a step in the daily job (after bars, AMT, Outcomes), with catch-up on startup.
- SUS = null in the MVP. Add a consensus source only if worst-miss reviews point at Scheduled Releases.
- Confounded Events stay in the Analog pool, flagged, with non-confounded ranked first.
- Hard monthly Opus budget cap in config ($100 to start), computed from the token usage the API reports. Scoring stops when hit; unscored Events are picked up after the cap resets. Spend is shown on the settings page.

Data:
- News: Alpaca News (Benzinga, history from 2015), the only source in the MVP. Verify in Phase 1 with a free key: full `content` field, volume, 2016 gap.
- Pre-filter: Article `symbols` ∩ holdings of the 8 ETFs, ∪ a macro keyword hit. No NER.
- Embeddings: local `sentence-transformers` (e.g. `bge-small`) in pgvector, for clustering and Analogs.
- VIX and the 10y yield: FRED (`VIXCLS`, `DGS10`), daily prior close.
- Scheduled Releases: FRED release dates (CPI, NFP, PCE, GDP), a hand-maintained FOMC list in config, FMP earnings calendar if the free tier covers it (otherwise earnings are left out of v1).
- Holdings: one source, refreshed monthly. FMP if the free tier covers `/etf/holdings`, else a manual issuer CSV. No `HoldingsProvider` chain.
- DB: Postgres + pgvector via docker compose from the first commit. No SQLite phase.

Cut from MVP (supersedes the watchlist toggle and holdings fallback sections above):
- Watchlist toggle / `data_only` / `ai_on` states. Use a fixed list of 8 ETFs + UUP/USO in config.
- Self-consistency (can return later as a paired A/B arm), intraday 1h horizon, alerts.
