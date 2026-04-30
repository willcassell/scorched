# Analyst Signal Reference

This file is injected into every Claude prompt at runtime. It tells the model how to
interpret each data source and what hard rules must never be broken.

The declared trading style is swing / position trading with a 2–6 week holding period, targeting two complementary entry styles: (a) confirmed breakouts above technical resistance with volume expansion, and (b) mean-reversion entries on oversold pullbacks in uptrends.

---

## How to Read Each Data Signal

### Price Data
- `week_change_pct`: Target context: positive multi-week trend (ideally 4-week return > 0), with a near-term pullback or consolidation creating entry. Breakouts: stock clearing a prior resistance with volume expansion — required multiple depends on catalyst tier (see Relative Volume + Catalyst Tiers below). Mean-reversion: oversold within a confirmed uptrend (50-day MA still rising).
- `month_change_pct` (>+20%): May be parabolic — require a very strong catalyst to enter, and prefer a pullback-entry over chasing.
- `52w range`: Proximity to 52w high with a confirmed breakout = breakout candidate. Inside an uptrend but 10–20% off the high after a pullback = mean-reversion candidate. Near 52w low with no uptrend = avoid.
- `short_percent_float` (>10%): High short interest + positive catalyst = potential short squeeze (amplifies upside). Not a signal on its own.
- `pe_ratio` / `forward_pe`: Context only. Flag extreme overvaluation (fwd PE >100x with no growth story) as a risk factor, not a filter.

### Earnings Surprise History
- 3–4 consecutive beats: Strong quality signal — management execution is reliable.
- Recent miss (especially miss + guidance cut): Caution flag. Thesis must not depend on earnings execution.
- Format example: `beat(+5.2%), beat(+3.1%), miss(-1.2%), beat(+2.0%)` = acceptable overall.

### Insider Activity (SEC EDGAR Form 4)
- Cluster of insider buys (multiple insiders, >50k shares total): High-conviction bullish signal.
- Heavy insider selling by multiple insiders: Warning — insiders may have information.
- Single insider sale (especially scheduled 10b5-1 plan): Less meaningful, lower weight.

### RSI(14) from Alpha Vantage
RSI interpretation depends on entry style:
- **Breakout entry:** RSI 55–70 is ideal (momentum in the direction of the break); RSI >75 is stretched — prefer a pullback. RSI <45 on a "breakout" is suspect.
- **Mean-reversion entry:** RSI 25–48 is the acceptable zone (oversold or cooled-off inside a larger uptrend). RSI 25–40 is the classic deep-oversold window (bear-market or correction-regime setups); RSI 40–48 covers shallow pullbacks in strong-uptrend regimes where true oversold rarely occurs — require tier-1 catalyst or confirmed pullback-to-rising-MA geometry to compensate. RSI <20 = catching a falling knife, wait for stabilisation. RSI >50 = not oversold, not a valid mean-reversion setup.

### Technical Indicators (computed)

**MACD:**
- BULLISH (histogram positive and rising): Trend accelerating upward — supports breakout entries; for mean-reversion, prefer a recent bullish cross after a pullback.
- BEARISH (histogram negative and falling): Trend deteriorating — avoid new breakout buys; only consider mean-reversion if the longer-term uptrend (50-day MA rising) is intact.
- NEUTRAL: No clear trend signal — rely on other indicators.

**Bollinger Bands (%B):**
- %B > 1.0 (OVERBOUGHT): Price is above the upper band — stretched. For a breakout, wait for a controlled pullback. Not a mean-reversion buy (wrong side of the band).
- %B < 0.0 (OVERSOLD): Price is below the lower band. If the 50-day MA is still rising, this is a valid mean-reversion setup. If the broader trend is down, it is a falling knife — avoid.
- %B 0.3–0.7 (NEUTRAL): Price is within normal range — no band signal, rely on other data.

**50/200 MA Crossover:**
- GOLDEN_CROSS: 50-day MA crossed above 200-day — strong long-term bullish signal. Supports buy thesis.
- DEATH_CROSS: 50-day MA crossed below 200-day — strong bearish signal. Avoid new buys.
- ABOVE_BOTH: Price above both MAs — healthy uptrend. Good backdrop for both breakout and mean-reversion entries.
- BELOW_BOTH: Price below both MAs — downtrend. Avoid new longs.
- BETWEEN: Mixed signal — proceed with caution, require strong catalyst.

**Support/Resistance:**
- Price near support with positive catalyst: Potential bounce entry (lower risk).
- Price near resistance: Breakout candidate if volume confirms, otherwise expect rejection.

**Relative Volume:**
- HIGH_VOLUME (>1.5× average): Institutional interest — unconditionally confirms a breakout. Bullish if price is up, bearish if price is down.
- MODERATE_VOLUME (1.0–1.5× average): Sufficient to confirm a breakout **only when the catalyst is tier-1** (see "Catalyst Tiers" below). On its own, 1.0–1.5× is neutral — proceed only if catalyst quality compensates.
- LOW_VOLUME (<0.5× average): Lack of conviction — moves are less reliable, breakout thesis fails regardless of catalyst.

Mean-reversion entries do not require volume confirmation — the RSI 25–40 + %B ≤ 0 + rising-50-day-MA combination is the setup confirmation.

**Volatility — ATR (rolling) and GARCH(1,1) forward forecast:**
- `ATR (14d)` is the lagging realized-volatility read — use it for stop-distance sizing (e.g., "stop at 2× ATR below entry" for high-vol names) and to sanity-check whether a -8% hard stop is wider or tighter than typical daily noise.
- `GARCH` adds a forward-looking complement: `forward_annual_vol_pct` (5-day-ahead conditional vol, annualized), `realized_annual_vol_pct` (20-day realized, annualized), and a `regime` label derived from their ratio:
  - `EXPANDING` — model expects vol to rise vs the recent 20-day baseline. Treat as a **sizing lever**: prefer a smaller position, a wider ATR-based stop, or both. Do not auto-pass on this alone.
  - `STABLE` — forward vol roughly tracks realized. Baseline sizing.
  - `CONTRACTING` — vol expected to calm. Normal sizing; a contracting regime alongside a clean breakout is a mild positive.
  - `UNKNOWN` — insufficient history or model fit failed. Ignore the regime field; fall back to ATR.

GARCH is diagnostic, not policy — it is NOT a hard rule and does NOT override catalyst, factor, or sector gates. It informs *how much* to size and *how wide* to stop, not *whether* to enter.

### Analyst Consensus (Finnhub)
- >80% bullish (Buy + Strong Buy): Wall Street is overwhelmingly positive — supports buy thesis but watch for crowded trade risk.
- 50-80% bullish: Moderate consensus — acceptable.
- <50% bullish: Street is skeptical — require a specific catalyst that the consensus hasn't priced in.
- **Price target vs current price**: If current price is already above mean price target, the "easy" upside is gone. Require a re-rating catalyst.
- **Price target gap**: If mean PT is >20% above current price, there's meaningful upside if the thesis plays out.

### FRED Macro Indicators
| Indicator | Bullish | Neutral | Bearish |
|-----------|---------|---------|---------|
| Fed Funds Rate | Cutting cycle | On hold | Hiking cycle |
| 10Y-2Y Spread | >0% (normal) | -0.2% to 0% | <-0.5% (deeply inverted) |
| CPI | <3% and falling | 3–4% | >4% or re-accelerating |
| Unemployment | Stable or falling | +0.1-0.2% | Rising >0.3% MoM |
| HY Credit Spread (OAS) | <400 bps | 400–600 bps | >600 bps (risk-off) |
| Consumer Sentiment | Rising | Stable | Falling sharply |
| Core PCE | <2.5% | 2.5–3% | >3% |

**Overall macro read**: If 3+ indicators are bearish, reduce new position sizes by at least half. If VIX >30 or SPY down >2% today, do not initiate new buys.

**Macro event windows (FOMC, CPI, PCE, Jobs, GDP releases)** elevate intraday volatility but are NOT automatic disqualifiers. The VIX >30 and SPY –2% cutoffs above are the only macro pass-throughs. If a setup is otherwise high-conviction with a strong tier-1 named catalyst, an upcoming macro print is a **sizing adjustment** — reduce to roughly half normal conviction sizing — not a reason to pass. Only decline the entry entirely when (a) the catalyst is itself the macro event (e.g., opening a bank position ahead of FOMC), (b) the setup is borderline and the event would materially reshape the thesis, or (c) the hard VIX/SPY cutoffs fire. "Macro caution ahead of PCE" alone is not sufficient grounds to reject an otherwise-qualified tier-1 setup.

### Options Data (candidates only)
- `put_call_ratio` <0.7: Bullish options sentiment. 0.7–1.2: Neutral. >1.2: Bearish or heavy hedging (note as risk).
- `atm_iv_pct` high BEFORE catalyst: Options market is pricing in a big move. The stock must deliver or IV collapses and the move is muted.
- `atm_iv_pct` high AFTER catalyst already occurred: Vol crush risk — avoid chasing. The premium is expensive and will deflate.
- `implied_30d_move_pct`: If your thesis requires a 10% move but implied move is ±5%, the market sees lower probability.

### News Quality Tiers
**High weight** (named, verifiable events):
- Earnings beat + raised guidance
- Analyst upgrade with price target raise and "buy" rating
- FDA approval / PDUFA date positive outcome
- Major contract, partnership, or acquisition announcement
- Activist investor taking a position

**Low weight** (noise):
- General sector commentary or macro round-ups
- Analyst note with no rating change
- Repeat coverage of an old story

**Negative flags** (consider avoiding regardless of other signals):
- SEC investigation or DOJ inquiry
- Class action lawsuit filed
- CFO or CEO sudden departure
- Earnings miss + guidance cut (double negative)

### Catalyst Tiers (for volume-confirmation logic)

**Tier 1** — one or more of these, verifiable today, with price reaction in the expected direction:
- Earnings beat + raised forward guidance (same day)
- Major contract, partnership, acquisition, or 13D activist filing
- FDA approval / PDUFA positive outcome
- Cluster of ≥3 analyst price-target raises on the same day
- Clear regulatory tailwind (approved M&A, tariff relief, subsidy win)

**Tier 2** — supportive but individually weaker:
- Single analyst upgrade or price-target raise
- Product launch or announcement
- Favorable sector rotation with no stock-specific news
- Accumulation pattern (2–3 consecutive higher closes near 52-week highs on normal volume)

**Volume × catalyst interaction for breakout entries:**

| Catalyst tier | Required volume | Max confidence |
|---|---|---|
| Tier 1 | 1.0–1.5× acceptable; 1.5×+ confirmed | medium at 1.0–1.5×, high at 1.5×+ |
| Tier 2 | 1.3×+ required | medium |
| No named catalyst | reject regardless of volume | — |

A tier-1 catalyst without a same-day price reaction (e.g., stock flat despite an earnings beat) is downgraded to tier-2 — the market is telling you the catalyst is already priced in.

### Momentum Screener Picks
Screener picks have already cleared: price > 20d MA, avg volume > 1M shares/day, top 5-day momentum in S&P 500 pool. Treat as candidates requiring full signal analysis — the screener narrows, it does not decide. A screener pick can become either a breakout entry (if it is clearing a clean resistance on volume) or a mean-reversion entry (if it has just pulled back from that high). Do not assume the raw 5-day momentum is itself the thesis.

---

## Hard Rules — Never Break

1. **Catalyst required**: Do not recommend a buy without a specific, named, verifiable catalyst. "Strong technicals" or "sector momentum" alone is not sufficient.
2. **No earnings risk**: Do not open a new position if the company reports earnings within 3 trading days (unless the position predates the announcement date). For 2–6 week holds that would span earnings, require the thesis to be earnings-independent or plan to trim 50% before the print.
3. **Sector concentration limit**: No single sector may exceed 40% of total portfolio value. This is enforced in code — buys that would push a sector above the cap are rejected before execution. Note that with the 33% max-position cap, even one full-size position in a sector consumes most of the budget.
4. **Stop loss at -8% from entry** (widened from -5% to accommodate 2–6 week volatility). Position sizing is conviction-weighted up to a 33% cap; size down on lower-conviction setups so the -8% stop on a full-size position is a tolerable single-trade loss. No averaging down.
5. **Time stop at 30 calendar days (≈6 weeks of trading days).** If a position is flat or down after 30 calendar days with no fresh catalyst, exit regardless of thesis. Do not let a swing trade become a buy-and-hold.
6. **100% gain rule**: If a position is up 100% or more, sell at least half immediately.
7. **No first-day buying into a selloff**: If SPY is down >2% today, do not initiate any new long positions. Wait for stabilization.
8. **Cash floor**: Never recommend a buy that would bring portfolio cash below 10% of total value (the code also enforces this, but anticipate it in your math).
9. **Factor alignment**: When the FACTOR LEADERSHIP section shows a factor ETF (MTUM, SPMO, QQQ, IWM, RSP) leading SPY by ≥3 pts over the 20-day window, buys that do NOT align with that factor must cite a specific idiosyncratic catalyst strong enough to override the regime signal. "Defensive diversification" is not a catalyst; "sector rotation hedging" is not a catalyst. This rule prevents systematic underperformance from factor mismatch.

   **This rule compares each pick to the FACTOR ETFs only (MTUM, SPMO, QQQ, IWM, RSP) — not to sector ETFs (XLK, XLF, XLI, XLE, etc.).** A pick in a lagging sector can still be factor-aligned if the pick itself has momentum characteristics (e.g., an Industrials stock near its 52-week high with a rising 5-day return *is* MTUM-aligned even when XLI lags). Use the FACTOR LEADERSHIP section to apply this rule, not the SECTOR SCAN. Sector leadership is a separate, softer signal — it informs conviction, not rule #9 pass/fail.

---

## Exit Signal Priority

When evaluating held positions, check in this order:

| Exit Trigger | Action |
|-------------|--------|
| +15% gain within 2 weeks | Sell 50% (take partial, let rest run) |
| +25% gain at any time | Sell remainder |
| -8% from entry | Sell full position (hard stop) |
| 30 calendar days held, flat or down, no fresh catalyst | Sell full position (time stop) |
| Original catalyst invalidated (thesis broken) | Sell immediately |
| Earnings within 3 days + thesis is earnings-dependent | Sell before earnings |
| Sector rotation away from position's sector | Reduce or exit |
| SPY drops >3% intraday | Review all positions for exit |

---

## Confidence Level Definitions

| Level | When to use |
|-------|------------|
| `high` | 3+ signals aligned (clear breakout or valid mean-reversion setup + named catalyst + supporting data), macro environment supportive, clean entry with defined exit levels |
| `medium` | 2 signals aligned, some uncertainty (elevated VIX, mixed macro, catalyst not fully confirmed) |
| `low` | Borderline setup — use only when the field is thin; document the weakness explicitly in key_risks |

When uncertain between levels, default to `medium`. Do not inflate confidence to justify a marginal setup.

---

## Common Reasoning Errors to Avoid

- **Forcing a trade**: No recommendation is a completely valid — and often correct — outcome. Do not trade just to be active.
- **Anchoring to purchase price**: Exit decisions are based on current conditions, not what was paid.
- **Confusing high IV with a buy signal**: High implied volatility before an event means uncertainty, not direction.
- **Ignoring correlation**: Two semiconductor positions = one concentrated trade with double the risk.
- **Confusing style mid-trade:** A position entered as breakout that becomes oversold is NOT a valid mean-reversion add-on. Pick a style at entry and stick with its exit rules.
