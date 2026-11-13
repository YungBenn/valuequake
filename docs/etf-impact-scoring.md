# ETF Impact Scoring (v2) for Claude Opus 5.5

Model: `claude-opus-5-5`
Put the SYSTEM PROMPT in the system field (cache it). Fill the USER MESSAGE template per article.
One call per Event (its earliest Article), scoring all candidate ETFs at once.

Part A is the label formula (computed in code). Part B is the prompt. Part C is evaluation and calibration.
All numbers in Part A are computed in code. Opus never calculates them.

---

# PART A: Impact label formula (ground truth)

## Why v1 was replaced
| v1 problem | Fix in v2 |
|---|---|
| `ETF - SPY` assumes beta = 1. Wrong for TLT, GLD (near zero or negative beta), SMH (beta > 1) | Market model with estimated beta |
| Divided by raw 20d vol, but numerator is abnormal return (different scale) | Divide by residual (abnormal) vol |
| 20d simple vol is noisy and step-changes when old shocks drop out | EWMA vol (lambda 0.94) with a long-run floor |
| "1 trading day after publish" was ambiguous (overnight vs intraday news) | Exact window rules by publish time |
| Window from prior close leaks pre-news move into the label | Start price = last price before publish |
| 30 articles on the same CPI print counted as 30 wins/losses | Evaluate per event cluster |
| No base rates given to model, so it anchors on nothing | Empirical base rates per ETF passed in prompt |
| Macro surprise passed as raw text | Standardized surprise computed in code |

## A1. Expected return model (market model)
For each ETF i, estimate on trading days [t-250, t-11] (excluding days that had high-impact clusters, if known):

```
R_i,d = alpha_i + beta_i * R_SPY,d + e_i,d      (OLS on daily log returns)
```
- Re-estimate weekly; use only data before the event (no lookahead).
- In practice set alpha_i = 0 for short windows (alpha is tiny and noisy).
- SPY itself: expected return = 0, so AR = raw return.
- Shrink noisy betas toward 1 for equity ETFs (e.g. beta_used = 0.7 * beta_ols + 0.3 * 1.0). Do not shrink for TLT/GLD.

## A2. Abnormal return over the event window
```
AR_i = R_i,[t0, t1] - beta_i * R_SPY,[t0, t1]
```
Same window for ETF and SPY, using prices from the 1-min bars.

## A3. Event window rules (t0, t1)
Let D = the first trading session whose close is after `published_at`.

| Publish time (ET) | t0 (start price) | t1 (end price) |
|---|---|---|
| Overnight / pre-market / weekend (16:00 prev to 09:30) | prior session close | close of D + (h - 1) |
| During RTH (09:30 to 16:00) | last 1-min close before publish | close of D + (h - 1) |
| After close (16:00 onward) | that day's close | close of next session + (h - 1) |

- Primary horizon h = 1 (to the close of D). Also store h = 3 and h = 5 outcomes for persistence/reversal tracking (useful for a daily trader).
- Pre-market news is included because the gap is part of the reaction.

## A4. Volatility scaling (standardized abnormal return, SAR)
Residual vol from EWMA on daily residuals e_i,d (lambda = 0.94, data up to D-1):
```
s2_d = 0.94 * s2_(d-1) + 0.06 * e_(d-1)^2
sigma_ewma = sqrt(s2)
sigma_long = std of e over [t-250, t-11]
sigma = max(sigma_ewma, 0.5 * sigma_long)     # floor stops tiny denominators in calm regimes
```
Scale for window length:
```
f = fraction of day D remaining after t0
    (1.0 if t0 is a prior close; else minutes_remaining / 390)
T = f + (h - 1)
SAR = AR / (sigma * sqrt(T))
```
Note: intraday volume/vol is U-shaped, so minutes/390 is an approximation. Fine for MVP.

## A5. Impact label
```
high:   |SAR| >= 2
medium: 1 <= |SAR| < 2
low:    |SAR| < 1
direction_realized = sign(AR)
```
Base rates come from placebo publish times (random timestamps per ETF, same A3 window rule and A4 SAR), bucketed by session timing (ADR 0002). Check them per ETF after backfill. Real returns are fat-tailed, so expect more "high" than the normal ~5%. If base rates are badly skewed (e.g. high > 15%), switch to per-ETF empirical thresholds (70th and 93rd percentile of placebo |SAR|) and document it. Same definition must be used in RAG analogs, labels, and the prompt.

## A6. Extra stored fields (not part of the label, used as features and diagnostics)
- `pre_sar`: SAR from 3 trading days before t0 up to t0 (pre-event drift). High |pre_sar| in the news direction = priced in or leaked.
- `abnormal_volume`: log(volume of D / median volume of last 20 sessions). A volume shock confirms news was absorbed even if price barely moved.
- `sar_h3`, `sar_h5`: for persistence. Reversal flag = sign(sar_h5) != sign(sar_h1) and |sar_h1| >= 1.
- `confounded`: true if another tier-1 scheduled event (CPI, NFP, FOMC, PCE, GDP, mega-cap earnings in holdings) falls inside [t0, t1].

## A7. Standardized macro surprise (scheduled releases only)
```
SUS = (actual - consensus) / std(historical surprises of that indicator)
```
Standard in the macro announcement literature. Compute in code from stored past releases; pass SUS + sign convention ("positive = hotter/stronger than expected") to Opus. If consensus is unavailable, pass `null`.

## A8. Event clustering
- Group articles about the same underlying event (embedding similarity + same entities + within 24h) into a cluster.
- Score each article, but evaluation uses one row per (cluster, ETF): take the earliest article's prediction.
- Analog pool also stores clusters, not every duplicate article.

---

# PART B: Prompt

## SYSTEM PROMPT

You are a market impact analyst. Your job is to estimate how strongly a single news article will move specific ETFs over a fixed horizon. You are not giving investment advice. Your probability estimates will be scored against real outcomes with a proper scoring rule, so honest calibration matters more than sounding confident. Overconfidence is penalized.

### Definition of impact (computed by the system, not by you)
For each ETF, the system measures the standardized abnormal return (SAR) from the last price before publish to the close at the end of the horizon:
- abnormal return = ETF return minus beta times SPY return (beta is provided; for SPY itself, the raw return)
- SAR = abnormal return / the ETF's typical abnormal move for that window length
- high: |SAR| >= 2, medium: 1 <= |SAR| < 2, low: |SAR| < 1

Because returns are beta-adjusted, a market-wide move does not count as impact for a sector ETF unless the ETF moves more or less than its beta implies. For SPY, any move counts.

### How to reason (for each ETF)
1. Start from the base rate. Each ETF comes with its empirical base rates (share of all days that were low/medium/high). Begin there and adjust only with specific evidence. Most news should end close to the base rate.
2. Relevance: How directly does this news touch the ETF? Consider holding weights (news on a 20% holding matters far more than a 0.5% holding), sector exposure, and macro sensitivity (rates, oil, USD, risk sentiment). Remember the beta adjustment: broad risk-on/off news mostly shows up in SPY, not as abnormal moves in sector ETFs, unless the ETF is unusually sensitive to that specific news.
3. Novelty: Genuinely new information, or a rehash, opinion piece, recap, or already-expected outcome? Recaps and "why X fell today" articles are almost always low impact.
4. Pre-event drift: If `pre_sar` is large in the news direction, the move may be priced in or leaked. Raise priced_in_risk and lean lower.
5. Surprise: For scheduled releases, impact depends on the standardized surprise (SUS), not the event itself. As a rough guide, |SUS| < 0.5 rarely matters, |SUS| > 1.5 often does. If SUS is null, say so and lower confidence.
6. Magnitude: Policy shifts, guidance changes, regulation, sanctions, supply shocks, and major M&A rank above routine updates, analyst notes, and commentary.
7. Dilution: Broad ETFs (SPY, IWM) need macro-level news to move. Single-company news rarely moves a diversified ETF unless the company is a large weight.
8. Analogs: Use the provided historical analogs and their realized SARs as evidence. Weight closer analogs more. If analogs disagree with your view, explain why you deviate.
9. Confounding: If other tier-1 events fall in the same window, the outcome may be driven by them. Note it and lower confidence. Do not raise impact just because a big event is nearby.
10. Auction context (AMT): Use the provided AMT state to judge how easily price can move. Never compute or alter levels yourself.
    - Inside a multi-day balance with mild news: likely absorbed, lean lower.
    - At a balance edge with strong news in the breakout direction: lean higher.
    - LVN or single prints between price and the next reference: moves travel fast, lean higher.
    - HVN or composite POC right at price: moves get absorbed, lean lower.
    - Already accepted outside value in the news direction before publish: raise priced_in_risk.
    - Poor high/low nearby: price may revisit it anyway; don't credit that move fully to the news.
    If no AMT state is provided, skip and note it in missing_info.

### Rules
- Use only the information provided. Do not invent facts, prices, consensus numbers, or events. List missing inputs in `missing_info` and widen uncertainty.
- Do not assume anything that happened after the article's publish time.
- Probabilities in steps of 0.05, each between 0.05 and 0.90 (never 0 or 1), summing to 1.00.
- `expected_abs_sar`: your point estimate of |SAR| (e.g. 0.4, 1.3, 2.6). Must be consistent with the probabilities.
- `direction`: expected sign of the abnormal return, "positive", "negative", or "unclear" (use unclear when mixed or interpretation-dependent).
- Keep `reasoning` to 2 to 4 concrete sentences referencing the factors above.
- Score every ETF in the candidate list, even if relevance is none (then stay at base rate).
- Output only valid JSON matching the schema. No markdown, no extra text.

### Output schema
{
  "article_id": "string",
  "article_summary": "string, one sentence, neutral",
  "event_type": "macro_data | central_bank | earnings | guidance | regulation | m_and_a | geopolitics | commodity_supply | legal | product | analyst_opinion | recap | other",
  "is_new_information": true,
  "is_scheduled_event": false,
  "scores": [
    {
      "etf": "string ticker",
      "relevance": "none | low | medium | high",
      "probabilities": { "low": 0.00, "medium": 0.00, "high": 0.00 },
      "expected_abs_sar": 0.0,
      "predicted_impact": "low | medium | high",
      "direction": "positive | negative | unclear",
      "confidence": "low | medium | high",
      "priced_in_risk": "low | medium | high",
      "deviation_from_base_rate": "string, which evidence moved you away from the base rate, or 'none'",
      "key_drivers": ["string"],
      "auction_context": "string, 1 sentence, or 'n/a'",
      "watch_levels": [ { "price": 0.0, "type": "string, from provided AMT refs only" } ],
      "analogs_used": ["analog_id"],
      "reasoning": "string"
    }
  ],
  "missing_info": ["string"]
}

`predicted_impact` must equal the level with the highest probability.
`confidence` reflects how much evidence you had (analog agreement, clarity of surprise, data completeness), not how big the move is.

---

## USER MESSAGE TEMPLATE

<article>
id: {{article_id}}
source: {{source}}
published_at_utc: {{published_at}}
session_timing: {{timing}}            <!-- "pre_market" | "rth" | "after_close" | "weekend" -->
headline: {{headline}}
body:
{{body}}
</article>

<horizon>{{horizon}}</horizon>        <!-- "to close of next session" for h=1 -->

<candidate_etfs>
{{#each etfs}}
- ticker: {{ticker}}
  name: {{name}}
  category: {{category}}
  top_holdings: {{holdings}}          <!-- "NVDA 20.1%, TSM 11.3%, AVGO 8.0%" -->
  sensitivities: {{sensitivities}}    <!-- "high rate sensitivity, USD negative" -->
  beta_vs_spy: {{beta}}
  base_rates: {{base_rates}}          <!-- "low 0.68, medium 0.24, high 0.08" from backfill -->
  pre_sar_3d: {{pre_sar}}             <!-- e.g. "+1.8" -->
  return_1d_before: {{ret_1d}}
  return_5d_before: {{ret_5d}}
{{/each}}
</candidate_etfs>

<market_snapshot as_of="{{published_at}}">
vix: {{vix}}
spy_return_1d_before: {{spy_ret_1d}}
us10y_yield: {{us10y}}
confounding_events_in_window: {{other_events}}   <!-- "FOMC decision 14:00 ET same day" or "none" -->
scheduled_release: {{release}}                   <!-- "CPI YoY: actual 3.3%, consensus 2.9%, SUS +2.1 (positive = hotter)" or "n/a" -->
</market_snapshot>

<auction_context as_of="{{published_at}}">
{{#each etfs}}
- ticker: {{ticker}}
  amt_state: {{amt_state_trimmed}}   <!-- location, acceptance, multi_day_state, nearest_refs -->
{{/each}}
</auction_context>

<historical_analogs>
{{#each analogs}}
- analog_id: {{id}}
  date: {{date}}
  headline: {{headline}}
  similarity: {{similarity}}          <!-- 0 to 1 -->
  confounded: {{confounded}}
  realized: {{realized}}              <!-- "SMH SAR -2.4, QQQ -1.1, SPY -0.6" -->
{{/each}}
</historical_analogs>

Score each candidate ETF for this article. Return JSON only.

---

# PART C: Evaluation and calibration

## Metrics (per ETF and overall, on cluster-level rows)
- **Ranked Probability Score (RPS)**, primary metric. Labels are ordered (low < medium < high); RPS penalizes predicting "low" when the outcome was "high" more than predicting "medium". Brier treats those the same.
- **RPSS** (skill score) vs the base-rate forecast: `1 - RPS_model / RPS_baserate`. Above 0 = Opus beats "just predict base rates". This is the main go/no-go number.
- **Log loss** (secondary, punishes confident misses).
- **Reliability diagram / ECE** per class (does "high 0.30" happen ~30% of the time?).
- **Spearman** between `expected_abs_sar` and realized |SAR| (ranking skill).
- **Direction hit rate**, only on rows where realized label != low and predicted direction != unclear.
- Slice by: event_type, confounded, timing, AMT on/off, prompt_version.

## Calibration layer
LLM verbalized probabilities are typically overconfident, so plan a post-hoc map:
- Collect at least ~300 to 500 labeled cluster rows (backfill helps).
- Fit a calibrator on Opus probabilities (start simple: temperature scaling on log-probabilities, or isotonic per class), on a time-based split (train on older, test on newer).
- Show calibrated probabilities in the app; keep raw ones stored.
- Refit monthly; check that calibration improves RPS on the holdout before deploying.

## Optional: self-consistency
For high-relevance rows only, run 3 calls and average probabilities. Usually improves calibration; costs 3x on those rows. Test with RPSS before enabling.

---

## Implementation notes
- Prompt caching: cache the system prompt (static). ETF profile block changes slowly, put it early so it can be cached too.
- Validate output with a JSON schema validator; on failure, retry once sending the validation error back. Then mark as failed.
- Also check: probabilities sum to 1 (±0.02), step of 0.05, min 0.05, predicted_impact matches argmax, all ETFs present, expected_abs_sar roughly consistent (e.g. > 2 only if high is the largest or second-largest class).
- Store raw model output + `prompt_version` + `label_version` with each prediction.
- Beta, sigma, base rates, pre_sar, SUS: all computed with data strictly before t0.
- Analogs must have `date < published_at`; prefer non-confounded analogs.
- Pass a trimmed AMT state (location, acceptance, multi_day_state, nearest_refs) to save tokens.
- Analogs can also store AMT state at publish time, so retrieval can match "similar news in similar structure".
- Historical Articles are not scored (ADR 0001). The Analog pool and Base Rates are built in code; calibration data comes from live Events only.
- Eval loop: weekly, pull worst misses (high RPS rows), review, update the prompt, bump version. Compare versions on the same holdout.
- Unit-test Part A formulas (TDD): window rules for each timing case, SAR scaling, EWMA recursion, floor, clustering.
