CREATE EXTENSION IF NOT EXISTS vector;

-- Prices are split-adjusted; close_raw is the unadjusted close.
CREATE TABLE IF NOT EXISTS bars_1m (
    ticker      text             NOT NULL,
    ts          timestamptz      NOT NULL,
    open        double precision NOT NULL,
    high        double precision NOT NULL,
    low         double precision NOT NULL,
    close       double precision NOT NULL,
    close_raw   double precision NOT NULL,
    volume      bigint           NOT NULL,
    trade_count integer          NOT NULL,
    vwap        double precision NOT NULL,
    PRIMARY KEY (ticker, ts)
);

CREATE TABLE IF NOT EXISTS job_runs (
    id           bigserial   PRIMARY KEY,
    session_date date        NOT NULL,
    step         text        NOT NULL,
    status       text        NOT NULL CHECK (status IN ('ok', 'failed')),
    counts       jsonb       NOT NULL DEFAULT '{}',
    error        text,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz NOT NULL
);

-- published_at is Alpaca's created_at from first ingest and is never overwritten.
CREATE TABLE IF NOT EXISTS articles (
    id           bigint      PRIMARY KEY,
    source       text        NOT NULL,
    headline     text        NOT NULL,
    summary      text        NOT NULL,
    body         text        NOT NULL,
    symbols      text[]      NOT NULL,
    published_at timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS articles_published_at ON articles (published_at);

-- Regular-hours open and close in UTC; half days have an early close_at.
CREATE TABLE IF NOT EXISTS trading_sessions (
    date     date        PRIMARY KEY,
    open_at  timestamptz NOT NULL,
    close_at timestamptz NOT NULL
);

-- Keyed on Alpaca's id: an ETF can pay income and capital gains on the same ex-date.
CREATE TABLE IF NOT EXISTS dividends (
    id      uuid             PRIMARY KEY,
    ticker  text             NOT NULL,
    ex_date date             NOT NULL,
    amount  double precision NOT NULL,
    special boolean          NOT NULL
);
CREATE INDEX IF NOT EXISTS dividends_ticker_ex_date ON dividends (ticker, ex_date);

-- One row per fully loaded month of Articles; the backfill skips these when resuming.
CREATE TABLE IF NOT EXISTS article_backfill_progress (
    month        date        PRIMARY KEY,
    articles     integer     NOT NULL,
    completed_at timestamptz NOT NULL DEFAULT now()
);

-- One row per fully loaded (ticker, month); the backfill skips these when resuming.
CREATE TABLE IF NOT EXISTS backfill_progress (
    ticker       text        NOT NULL,
    month        date        NOT NULL,
    bars         integer     NOT NULL,
    completed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, month)
);

-- Daily FRED series (VIXCLS, DGS10); days FRED reports as missing are not stored.
CREATE TABLE IF NOT EXISTS macro_series (
    series_id text             NOT NULL,
    date      date             NOT NULL,
    value     double precision NOT NULL,
    PRIMARY KEY (series_id, date)
);

-- CPI, NFP, GDP, PCE (source fred:<release id>) and FOMC decisions (source config).
CREATE TABLE IF NOT EXISTS scheduled_releases (
    name        text        NOT NULL,
    released_at timestamptz NOT NULL,
    source      text        NOT NULL,
    PRIMARY KEY (name, released_at)
);

-- Label counts of placebo Outcomes per (ETF, timing bucket, Scheduled Release in the h=1 window), one set per
-- monthly refresh (window_end). Shares are derived so they can never disagree with the counts.
CREATE TABLE IF NOT EXISTS base_rates (
    etf               text        NOT NULL,
    timing            text        NOT NULL,
    release_in_window boolean     NOT NULL,
    label_version     text        NOT NULL,
    window_start      date        NOT NULL,
    window_end        date        NOT NULL,
    seed              bigint      NOT NULL,
    n                 integer     NOT NULL,
    low               integer     NOT NULL,
    medium            integer     NOT NULL,
    high              integer     NOT NULL,
    share_low         double precision GENERATED ALWAYS AS (low::float8 / n) STORED,
    share_medium      double precision GENERATED ALWAYS AS (medium::float8 / n) STORED,
    share_high        double precision GENERATED ALWAYS AS (high::float8 / n) STORED,
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (etf, timing, release_in_window, label_version, window_end)
);
