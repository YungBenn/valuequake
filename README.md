# Valuequake

Estimates how strongly news will move specific ETFs, and measures those estimates against what actually happened.

Valuequake = value (Auction Market Theory value area) + quake (news shock). It is a personal research tool, not a trading bot: it never places orders.

The question it is built to answer: can Claude Opus predict the impact of news on an ETF better than a calendar-aware baseline?

## Status

**Phase 1** (current): market data, Outcome engine, Base Rates and Article collection. No UI and no LLM calls yet. Everything is inspected from Jupyter notebooks.

- Backfills 5 years of 1-min SIP bars, dividends and trading calendar from Alpaca, then keeps them current every session.
- Collects every Alpaca News Article, live and 5 years of history.
- Collects daily VIX and 10y yield from FRED, plus a calendar of Scheduled Releases (CPI, NFP, GDP, PCE, FOMC).
- Computes the Outcome for any (ETF, publish time): beta-adjusted abnormal return, standardized abnormal return (SAR) and a low/medium/high impact label for 1, 3 and 5 day horizons.
- Computes Base Rates from random placebo publish times through the same engine, split by whether a Scheduled Release falls in the window (the Calendar Baseline).
- Runs an hourly scheduler that catches up on missed sessions after the laptop wakes.

Later phases add Opus scoring with retrieved Analogs, and an Auction Market Theory panel. See [`docs/project-brief.md`](docs/project-brief.md) and [`docs/specs/phase-1.md`](docs/specs/phase-1.md).

ETF universe (in [`config.toml`](config.toml)): SPY, QQQ, SMH, XLE, XLF, TLT, GLD, IWM, plus UUP and USO as sensitivity tickers. SPY is the benchmark for beta and abnormal returns.

## Setup

Requires Docker, and [uv](https://docs.astral.sh/uv/) for local development.

```bash
cp .env.example .env   # fill in ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY, FRED_API_KEY
docker compose up -d   # Postgres (pgvector) + the hourly scheduler
docker compose run --rm app python -m valuequake backfill   # 5 years of history, resumable
```

## CLI

```bash
python -m valuequake                     # hourly scheduler (default, what the container runs)
python -m valuequake backfill [--years N]  # bars, dividends, Articles, FRED series, Scheduled Releases
python -m valuequake daily YYYY-MM-DD    # run the daily job once for one session
python -m valuequake gaps [--min-coverage 0.95]  # sessions with missing or partial bars
python -m valuequake base-rates          # recompute Base Rates now
```

Outside Docker, set `DATABASE_URL` (for example `postgresql+psycopg://valuequake:valuequake@localhost:5432/valuequake`) and run commands with `uv run`. `VALUEQUAKE_CONFIG` points at a different config file.

## Development

```bash
uv sync
uv run pytest        # integration tests start Postgres via testcontainers, so Docker must be running
uvx ruff check .
uv sync --group notebooks && uv run jupyter lab notebooks/
```

## Docs

- [`GLOSSARY.md`](GLOSSARY.md): the domain vocabulary (Article, Event, Prediction, Outcome, Base Rate, ...).
- [`docs/etf-impact-scoring.md`](docs/etf-impact-scoring.md): how Outcomes are defined and how Opus will score news.
- [`docs/amt-module.md`](docs/amt-module.md): the planned Auction Market Theory module.
- [`docs/adr/`](docs/adr/): architecture decisions.
