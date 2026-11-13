# Valuequake

Estimates how strongly news will move specific ETFs, and measures those estimates against what actually happened.

## News

**Article**:
One ingested news item with its source and publish time.
_Avoid_: Story, headline, post

**Event**:
A real-world happening covered by one or more Articles; its time is the earliest Article's publish time.
_Avoid_: Cluster, news event, story

## Scoring

**Prediction**:
Opus's impact estimate for one Event and one ETF, made from the Event's earliest Article.
_Avoid_: Score, forecast, signal

**Outcome**:
The realized impact for one Event and one ETF over the horizon.
_Avoid_: Result, label, ground truth

**Analog**:
A past Event with its Outcomes, given to Opus as evidence when scoring a new Article.
_Avoid_: Similar event, precedent, example

**Base Rate**:
The share of low, medium and high impact one ETF shows at random publish times with no news selected.
_Avoid_: Prior, unconditional rate

**Scheduled Release**:
A tier-1 event known in advance (CPI, NFP, PCE, GDP, FOMC decision, mega-cap earnings) that can move ETFs on its own.
_Avoid_: Macro event, catalyst, data release

**Calendar Baseline**:
Base Rates split by whether a tier-1 scheduled release falls inside the window; the forecast Opus must beat.
_Avoid_: Naive baseline, benchmark
