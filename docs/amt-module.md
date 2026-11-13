# AMT Module v2: Auction Market Theory Technical Analysis

Part of Valuequake (ETF News Impact Analyzer). Feed into `/grill-with-docs` together with `project-brief.md` and `etf-impact-scoring.md`.

## Why AMT here

News tells you WHAT might move the ETF. AMT tells you WHERE the market is in its auction, so you know if price is likely to move easily (thin structure, imbalance) or get absorbed (balance, strong value). Same news, different structure = different impact.

Two uses:

1. **Standalone TA panel**: daily auction read per ETF (value, balance/imbalance, key reference levels).
2. **Context for news scoring**: a trimmed AMT state is passed into the Opus impact prompt.

## Core principles

- All AMT numbers are computed deterministically in code. Opus never calculates levels.
- Opus only interprets: reads the computed state, writes the auction narrative, uses it when scoring news.
- Every AMT "tendency" (80% rule, naked POC magnet, LVN travel...) is a hypothesis until it beats a placebo baseline in backtest (Section 9). Only measured hit rates get shown to Opus and to the user.
- Scope: AMT + Market Profile (TPO) + volume profile from 1-min bars. Order flow (tick data, bid/ask, delta, footprint, DOM) is OUT OF SCOPE: user trades the daily timeframe, order flow is an intraday execution tool.

## What changed from v1

| v1 problem                                                                                                            | Fix in v2                                                                                                       |
| --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| TPO and volume value areas used interchangeably                                                                       | Two separate profiles with prefixed fields (`tpo_*`, `vol_*`) and a fixed rule for which one feeds which signal |
| Fixed bin size (0.05) across ETFs priced $90 to $600+                                                                 | ATR-based row size per ETF, fixed per month for comparability                                                   |
| Closing auction prints (about 9% of daily volume, up to ~20% on rebalance/expiry days) create a fake HVN at the close | Auction handling rule + data-quality flag                                                                       |
| Dividends (TLT monthly) shift price, so old levels and naked POCs drift                                               | Split-adjusted bars + dividend shift for carried-forward levels                                                 |
| HVN/LVN, balance, single prints defined loosely                                                                       | Exact algorithms with tunable parameters                                                                        |
| Day type had no thresholds                                                                                            | Numeric classifier (IB share of range, extension side, close location)                                          |
| "Who is in control" had no computed input                                                                             | Rotation factor + profile shape + value migration                                                               |
| Balance = "3 overlapping VAs"                                                                                         | Bracket detection with breakout / acceptance / failed-breakout states                                           |
| Mostly intraday concepts for a daily trader                                                                           | Daily structure first: daily/weekly one-timeframing, brackets, bracket composite, weekly profile                |
| 80% rule and other heuristics trusted as folklore                                                                     | Placebo-controlled backtests; only measured rates are used                                                      |
| Distances in % only                                                                                                   | Distances in ATR units (comparable across ETFs)                                                                 |

---

## 1. Data

### Source: Alpaca (free plan), decided

- Daily timeframe trading, so end-of-day processing is enough. No real-time feed.
- Free plan gives full-market SIP historical bars when the query end time is older than 15 min. Always pass `feed=sip` explicitly (IEX is a single exchange and gives wrong profiles).
- Pull 1-min bars (OHLCV + bar VWAP + trade count). Daily bars have no volume-at-price, so profiles need intraday bars. Derive 30-min bars for TPO.
- Request split-adjusted prices (check the bars endpoint `adjustment` parameter in Alpaca docs; store the raw close too for display).
- Use Alpaca's calendar endpoint for holidays and half days.
- Schedule: AMT job at ~16:30 ET for all ETFs. One-time backfill (paginated, respect rate limits).

### Session rules

- **RTH only (09:30 to 15:59 bars)** for all profiles. Decided. Pre-market is used only for gap size and open location vs prior value.
- **Auction handling**: the closing auction prints in one shot at the close price. Exclude bars timestamped 16:00 or later from the profile. Store `auction_volume_ratio = (16:00 bar volume) / RTH volume` as its own feature (higher closing-auction share relates to rebalancing and investor disagreement in the literature).
- **Quality flags** per session: `half_day`, `month_end`, `opex` (third Friday), `index_rebalance` (manual calendar), `vpoc_at_close` (VPOC within 1 row of the close). Flagged sessions get lower weight in composites and backtests.
- **Dividends**: on ex-dividend dates, shift every carried-forward level (prior POC/VAH/VAL, naked POCs, bracket edges, composites) down by the dividend amount, so old references stay on the same economic price.

## 2. Profile construction

### Row (bin) size

```
row = round_to_cent( ATR14_daily / 50 )      # session profile, about 50 rows per average day
row_composite = round_to_cent( ATR14_daily / 25 )
min row = 0.01
```

Recompute once per month per ETF (not daily), so profiles stay comparable within the month.

### Volume profile (vol\_\*)

- Each 1-min bar's volume is spread evenly across rows between its low and high (standard method). 1-min bars usually span 1 to 3 rows, so the distribution method barely matters. Optional later: triangular kernel centered on bar VWAP.
- **vol_POC**: row with max volume. Tie: the one closest to the range midpoint.
- **vol_VA (70%)**: see Value Area algorithm.

### TPO profile (tpo\_\*)

- 30-min periods A, B, C... (13 in a full RTH session).
- A row gets one TPO for each period whose high-low touched it.
- **tpo_POC**: row with most TPOs. Tie: closest to range midpoint.
- **tpo_VA (70%)**: same algorithm on TPO counts.

### Value Area algorithm (CBOT pair method)

```
1. VA = {POC}, total_in = count(POC)
2. up2 = sum of next 2 rows above VA; dn2 = sum of next 2 rows below VA
3. add the larger pair (both rows); if equal, add both pairs
4. if one side is exhausted, keep adding from the other side
5. stop when total_in >= 70% of total
VAH = top row of VA, VAL = bottom row of VA
```

(Pair vs single-row expansion differs by a row at most; pick pairs and keep it consistent.)

### Which profile feeds which signal

| Signal                                                                                                         | Profile | Why                                              |
| -------------------------------------------------------------------------------------------------------------- | ------- | ------------------------------------------------ |
| Value migration, bracket overlap, 80% rule, day type, single prints, tails, poor extremes, IB, rotation factor | TPO     | Classic Market Profile definitions are TPO-based |
| HVN / LVN, VPOC, naked VPOCs, composites                                                                       | Volume  | Where real participation sits                    |

Never mix a TPO POC with a volume VAH in one comparison without labeling both.

## 3. Session structure (per ETF, per session)

### Initial Balance

- `ib_high`, `ib_low` = range of periods A + B (first 60 min). Half days: same 60 min.
- `ib_ratio = ib_range / median(ib_range, 20 sessions)`
- `ib_share = ib_range / session_range`
- `range_ext_up = max(0, high - ib_high) / ib_range`, same for down.

### Tails, single prints, poor extremes (TPO)

- **Tail (excess)** at an extreme: contiguous rows at the high (or low) with TPO count = 1, length >= 2 rows.
- **Poor high / poor low**: the extreme row has TPO count >= 2 (no tail). Unfinished auction.
- **Weak extreme**: tail of exactly 1 row.
- **Single-print zone**: contiguous rows with TPO count = 1, strictly inside the range (not touching an extreme), length >= 2 rows. Store [low, high].

### Rotation factor (TPO, CBOT definition)

For each period after A: +1 if its high > prior period high, -1 if lower, 0 if equal; same for lows. Sum over the session.

- `rf_norm = RF / (2 * (n_periods - 1))` in [-1, +1]
- |rf_norm| >= 0.4 = one-sided rotation; below = two-sided

### Profile shape (volume profile)

Let `poc_pos = (vol_POC - low) / range`, `va_width = (vol_VAH - vol_VAL) / range`.
| Shape | Rule |
|---|---|
| P | poc_pos >= 0.65 and VA in upper half |
| b | poc_pos <= 0.35 and VA in lower half |
| thin / elongated | va_width < 0.35 and no dominant node (trend-like) |
| B (double distribution) | two HVN peaks separated by an LVN with depth < 0.3 of the smaller peak, or by a single-print zone |
| D (normal) | otherwise |

### Day type classifier (heuristic thresholds, tune in backtest)

Evaluate in this order; first match wins:

1. **Non-trend**: session_range < 0.5 \* median_range_20 and no range extension.
2. **Double distribution trend**: shape B and one-sided extension.
3. **Trend**: ib_share < 0.25, one-sided extension, close within 10% of the extreme, rf_norm one-sided.
4. **Neutral extreme**: extension on both sides, close in outer 15% of range.
5. **Neutral center**: extension on both sides, close in middle 50% of range.
6. **Normal variation**: ib_share between 0.25 and 0.65, one-sided extension.
7. **Normal**: ib_share >= 0.65 (IB holds most of the range).
   Store `day_type` and the raw inputs, so the rules can be retuned without recomputing.

### Open type (lower priority for a daily trader)

- `open_location`: above prior tpo_VAH / inside prior VA / below prior tpo_VAL / outside prior range.
- `open_type` from periods A and B: open drive (one-directional from the open, never returns to open price), open test drive (tests one way then drives the other), open rejection reverse, open auction (rotates around open). Optional for MVP.

### Volume context

- `rel_volume = RTH volume / median(RTH volume, 20 sessions)`
- `auction_volume_ratio` (see Section 1)

## 4. Daily / multi-day structure (primary for the user)

### Value migration (TPO VA, today vs prior session)

| Category           | Rule                                                  |
| ------------------ | ----------------------------------------------------- |
| higher             | VAL*t > VAH*(t-1)                                     |
| lower              | VAH*t < VAL*(t-1)                                     |
| overlapping_higher | VAH*t > VAH*(t-1) and VAL*t >= VAL*(t-1), overlapping |
| overlapping_lower  | VAL*t < VAL*(t-1) and VAH*t <= VAH*(t-1), overlapping |
| inside             | VAH*t <= VAH*(t-1) and VAL*t >= VAL*(t-1)             |
| outside            | VAH*t > VAH*(t-1) and VAL*t < VAL*(t-1)               |

Also store `migration_streak`: consecutive sessions of higher/overlapping_higher (positive) or lower/overlapping_lower (negative).

### One-timeframing (daily and weekly bars)

- Daily OTF up: each day's low > prior day's low (no lower low). Down: each day's high < prior day's high.
- `otf_daily = +n / -n / 0` (consecutive days), same for weekly bars.
- OTF is the daily trader's cleanest "one side in control" signal; a break of OTF is a warning, not a reversal by itself.

### Bracket (balance area) detection

```
state: no_bracket | in_bracket | breakout_pending | accepted_out | failed_breakout

start a candidate bracket on a session; extend it day by day while:
  - the new session's tpo_VA overlaps the bracket's value zone (union of member VAs), and
  - the close is within [bracket_low - 0.25*ATR, bracket_high + 0.25*ATR]
bracket is valid when it has >= 3 sessions

breakout_pending: close outside the bracket by >= 0.25 * ATR
accepted_out:     next session's tpo_VA has >= 50% of its width outside the bracket (value moved)
failed_breakout:  within 2 sessions of breakout_pending, close back inside the bracket
                  (classic "look above and fail" / "look below and fail")
```

Store bracket high/low, value zone, age in sessions, number of edge tests.

### Composites (volume profile)

- **Bracket composite**: all sessions in the current bracket (preferred: structure-defined, not arbitrary).
- **20-session** and **60-session** rolling composites.
- **Weekly profile** (current and prior week).
- Each gives vol_POC, vol_VAH, vol_VAL, HVN list, LVN list.

### HVN / LVN detection (composites and session)

```
1. smooth row volumes with a Gaussian kernel (sigma = 2 rows)
2. HVN = local maxima with prominence >= 10% of the max smoothed volume
3. LVN = local minimum between two adjacent HVNs where
         volume_at_min / min(peak_left, peak_right) <= 0.5
4. merge nodes closer than 3 rows; store [low, high] zone + center
```

### Naked POCs

- Track both naked vol_POC and naked tpo_POC per session (and weekly).
- Naked = no later session's high-low range has touched it.
- Keep only if age <= 20 sessions and distance <= 3 ATR (avoid clutter).

### Acceptance vs rejection

- **Daily (primary)**: close beyond a reference (prior VA edge, bracket edge, composite VA edge) AND next session's tpo_VA mostly beyond it = accepted. Probe beyond and close back inside = rejected.
- **Intraday (secondary)**: >= 2 consecutive TPO periods beyond a reference = accepted; return within 1 period = rejected.

### 80% rule (precise definition, hypothesis)

Open outside prior tpo_VA, then two consecutive 30-min periods trade inside prior VA → target: opposite prior VA edge in the same session. Traditional sources caution that the hit rate depends on distance from value, VA width, and market direction. Measure it (Section 9) before showing it.

## 5. AMT state object (per ETF per session, and snapshot at any news timestamp)

```json
{
  "etf": "SMH",
  "session_date": "2026-10-27",
  "as_of_utc": "2026-10-27T20:30:00Z",
  "price": 251.3,
  "atr14": 4.1,
  "row_size": 0.08,
  "quality_flags": ["month_end"],
  "session": {
    "tpo": { "poc": 250.8, "vah": 252.2, "val": 249.5 },
    "vol": { "poc": 250.85, "vah": 252.1, "val": 249.6 },
    "ib": {
      "high": 251.9,
      "low": 249.8,
      "ib_ratio": 0.8,
      "ib_share": 0.55,
      "ext_up": 0.4,
      "ext_down": 0.0
    },
    "day_type": "normal_variation_up",
    "shape": "P",
    "rotation_factor": 9,
    "rf_norm": 0.41,
    "tails": { "high": "poor", "low": "excess" },
    "single_prints": [[249.9, 250.3]],
    "rel_volume": 1.35,
    "auction_volume_ratio": 0.11
  },
  "daily_structure": {
    "value_migration": "overlapping_higher",
    "migration_streak": 2,
    "otf_daily": 3,
    "otf_weekly": 1,
    "bracket": {
      "state": "breakout_pending",
      "high": 250.5,
      "low": 245.1,
      "age": 6,
      "edge_tests": 3
    },
    "composite_bracket": { "poc": 247.9, "vah": 249.8, "val": 246.0 },
    "composite_20d": { "poc": 248.4, "vah": 251.0, "val": 244.2 }
  },
  "location": {
    "vs_prior_value": "above_vah",
    "vs_bracket": "above",
    "vs_composite_20d_value": "above_vah"
  },
  "acceptance": "pending",
  "nearest_refs": [
    { "type": "LVN_20d", "zone": [253.1, 253.7], "distance_atr": 0.44 },
    { "type": "naked_vpoc", "price": 255.1, "age": 9, "distance_atr": 0.93 },
    { "type": "bracket_high", "price": 250.5, "distance_atr": -0.2 },
    { "type": "HVN_20d", "zone": [247.6, 248.3], "distance_atr": -0.83 }
  ]
}
```

Snapshot rules (no lookahead):

- Only bars before `published_at`, and with Alpaca free plan only bars older than 15 min.
- News outside RTH = last completed session state. News during RTH = last completed session + partial session up to `min(published_at, now - 15 min)`.
- Trimmed version for the news scoring prompt: `atr14`, `location`, `acceptance`, `daily_structure.bracket.state`, `value_migration`, `otf_daily`, top 4 `nearest_refs`.

## 6. How AMT changes news impact (hypotheses, validated in Section 9)

- Inside a mature bracket, mild news → absorbed, lower impact.
- At a bracket edge with strong news in the breakout direction → higher impact.
- LVN zone between price and the next reference → faster travel, higher impact.
- HVN / composite POC at price → absorbed, lower impact.
- Already accepted beyond value in the news direction before publish → priced-in risk up.
- Poor high/low nearby → price may revisit it regardless; do not credit that move fully to the news.
- Low rel_volume → fragile: can move more, may not hold.
- Strong OTF in the news direction → continuation more likely; news against a long OTF run → watch for OTF break.

## 7. Outcome tracking (extends the scoring outcome job)

For each prediction and each session, record:

- Did price accept beyond prior value / bracket within the horizon (which side)?
- Did it touch each flagged reference (naked POC, LVN far edge, bracket edge) within 1, 3, 5 sessions?
- Day type and value migration of the next session.
- Bracket state transitions (breakout_pending → accepted_out / failed_breakout).
  These feed Section 9 and the AMT on/off A/B test in scoring (measured by RPSS).

## 8. AMT narrative prompt (Opus 5.5, daily per ETF, after close)

### SYSTEM PROMPT

You are an auction market theory analyst writing a daily-timeframe read for one ETF. You receive a fully computed AMT state, the last 5 sessions summary, and measured historical hit rates for this ETF's patterns. Do not compute or change any levels; use only the numbers given. Describe the auction plainly: where value is and where it is migrating, whether the market is in a bracket (balance) or trending (imbalance), who is in control based on the computed evidence (value migration, one-timeframing, rotation factor, profile shape, tails), what is unfinished (poor highs/lows, single prints, naked POCs), and which reference levels matter next. When you mention a tendency (e.g. naked POC revisits), cite the provided measured hit rate; if no rate is provided for a pattern, do not claim it as a tendency. Give conditional scenarios, each with an invalidation level. This is analysis, not investment advice. Output valid JSON only.

### OUTPUT SCHEMA

```json
{
  "etf": "string",
  "session_date": "YYYY-MM-DD",
  "market_state": "bracket | breakout_pending | trending_up | trending_down | failed_breakout",
  "control": "buyers | sellers | two_sided",
  "control_evidence": ["string, cite computed fields"],
  "summary": "string, 2 to 3 sentences",
  "unfinished_business": ["string"],
  "key_levels": [
    {
      "price": 0.0,
      "zone": [0.0, 0.0],
      "type": "string",
      "distance_atr": 0.0,
      "why": "string"
    }
  ],
  "scenarios": [
    {
      "if": "string condition at daily level, e.g. closes back inside the bracket",
      "then": "string expected auction behavior",
      "target_ref": 0.0,
      "invalidation": 0.0,
      "supporting_stat": "string, measured hit rate or 'none'"
    }
  ],
  "confidence": "low | medium | high",
  "missing_info": ["string"]
}
```

Constraints: 3 to 6 key levels, 2 to 3 scenarios, all prices must come from the provided state.

### USER MESSAGE TEMPLATE

```
<amt_state>
{{amt_state_json}}
</amt_state>
<prior_5_sessions>
{{prior_sessions}}   <!-- date, tpo POC/VAH/VAL, day_type, value_migration, bracket state -->
</prior_5_sessions>
<measured_stats etf="{{etf}}" window="{{backtest_window}}">
{{stats}}            <!-- e.g. "naked VPOC within 1 ATR touched in 5 sessions: 64% (placebo 41%, n=212)" -->
</measured_stats>
Write the daily auction read. JSON only.
```

## 9. Validation plan (placebo-controlled)

Price always wanders near some level, so a "level got touched" stat means nothing without a baseline. For every heuristic, compare against **placebo levels**: random prices at the same ATR distance and age, drawn from the same sessions.

| Hypothesis                               | Metric                                            | Baseline                                |
| ---------------------------------------- | ------------------------------------------------- | --------------------------------------- | ------------------------- | ---------------------- |
| Naked POC acts as a magnet               | P(touch within 1/3/5 sessions)                    | placebo level, same distance/age        |
| Poor high/low gets revisited             | P(revisit within 5 sessions)                      | non-poor extremes, same distance        |
| LVN = fast travel                        | sessions (or 30-min periods) to traverse the zone | equal-width non-LVN zone                |
| HVN = absorption                         |                                                   | SAR                                     | of news landing at an HVN | news landing elsewhere |
| Bracket breakout accepted → continuation | 5-day beta-adjusted return                        | all breakouts_pending                   |
| Failed breakout → move to the other edge | P(touch opposite bracket edge within 5 sessions)  | placebo edge distance                   |
| 80% rule                                 | P(opposite VA edge same session)                  | unconditional P from same open location |
| OTF continuation                         | P(OTF extends next day)                           | unconditional daily P                   |

Rules:

- Time-based split (fit thresholds on older data, test on newer).
- Report n, hit rate, placebo rate, and a simple significance check (two-proportion test or bootstrap CI).
- A heuristic is "validated" only if it beats placebo out of sample. Only validated stats go into `measured_stats` and into the news scoring prompt.
- Re-run quarterly.

## 10. Web app additions

- ETF page: daily candles + volume profile side panel (session, bracket composite, 20d), VAH/VAL/POC lines, naked POC markers, bracket box with state badge, LVN/HVN zones shaded.
- TPO view (letters per 30 min), IB shaded, single prints and tails highlighted.
- Daily auction read card with scenarios and invalidation levels.
- News markers on the chart showing the AMT state at publish time.
- Stats page: validated heuristic hit rates vs placebo per ETF.
- Charting (MVP, Streamlit + Plotly): candles + horizontal volume profile bars as a side subplot, shapes for VA/bracket boxes. TPO letters as a Plotly text grid or heatmap. If the UI moves to a web frontend later, TradingView Lightweight Charts has an official volume profile plugin example.

## 11. Implementation notes

- AMT engine = pure functions: bars in, state out. Ideal for TDD with hand-built fixture sessions (known POC/VA, a trend day, a neutral day, a poor high, a bracket breakout, a failed breakout, an ex-dividend shift).
- Engine is Python (same package used by the daily job and the backtests). Start from or cross-check against `py-market-profile` (POC, VA, IB, HVN/LVN, vol and TPO modes); extend it for brackets, OTF, rotation factor, day type, ATR rows, auction exclusion.
- Store all thresholds in one config file with a `amt_config_version`; save it with every state so backtests are reproducible.
- Recompute history when the config changes (states are cheap to rebuild from stored 1-min bars).

## 12. Decisions (grilling 2026-10-28)

- Data provider: Alpaca free, `feed=sip`, end-of-day. Backfill: 5 years of 1-min bars.
- Session scope: RTH only; pre-market only for gap/open location. Order flow out of scope.
- Composites: bracket + 20d + 60d + weekly. No monthly.
- Thresholds: accept the v2 defaults, tune after the placebo backtest.
- Index rebalance ulag: skipped in the MVP (`month_end`/`opex` cover most of it).
- AMT in scoring: decided by the paired AMT on/off RPSS test (Phase 4, ADR 0003).
