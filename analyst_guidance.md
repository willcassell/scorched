# Analyst Signal Reference

This file is injected into every Claude prompt at runtime. It tells the model how to
interpret each data source and what hard rules must never be broken.

---

## How to Read Each Data Signal

### Price Data
- `week_change_pct` (+3% to +8%): Target momentum zone. <+3% = insufficient momentum. >+8% = potentially overextended.
- `month_change_pct` (>+20%): May be parabolic — require a very strong catalyst to enter.
- `52w range`: Proximity to 52w high with positive momentum = breakout candidate. Near 52w low = avoid (wrong direction).
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
- 40–65: Healthy momentum range. Fine to enter.
- 65–70: Approaching overbought. Tradeable with a strong catalyst; lower confidence.
- >70 (OVERBOUGHT): Expect a pullback. Require exceptional catalyst; note in key_risks.
- <30 (OVERSOLD): Wrong direction for momentum strategy. Avoid.

### Technical Indicators (computed)

**MACD:**
- BULLISH (histogram positive and rising): Momentum is accelerating upward — supports buy entries.
- BEARISH (histogram negative and falling): Momentum is deteriorating — avoid new buys, consider exits.
- NEUTRAL: No clear momentum signal — rely on other indicators.

**Bollinger Bands (%B):**
- %B > 1.0 (OVERBOUGHT): Price is above the upper band — overextended, expect a pullback. Lower confidence on new buys.
- %B < 0.0 (OVERSOLD): Price is below the lower band — wrong direction for momentum strategy. Avoid.
- %B 0.3–0.7 (NEUTRAL): Price is within normal range — no band signal, rely on other data.

**50/200 MA Crossover:**
- GOLDEN_CROSS: 50-day MA crossed above 200-day — strong long-term bullish signal. Supports buy thesis.
- DEATH_CROSS: 50-day MA crossed below 200-day — strong bearish signal. Avoid new buys.
- ABOVE_BOTH: Price above both MAs — healthy uptrend. Good for momentum entries.
- BELOW_BOTH: Price below both MAs — downtrend. Avoid.
- BETWEEN: Mixed signal — proceed with caution, require strong catalyst.

**Support/Resistance:**
- Price near support with positive catalyst: Potential bounce entry (lower risk).
- Price near resistance: Breakout candidate if volume confirms, otherwise expect rejection.

**Relative Volume:**
- HIGH_VOLUME (>1.5x average): Institutional interest — confirms moves. Bullish if price is up, bearish if price is down.
- LOW_VOLUME (<0.5x average): Lack of conviction — moves are less reliable.

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

### Momentum Screener Picks
Screener picks have already cleared: price > 20d MA, avg volume > 1M shares/day, top 5-day momentum in S&P 500 pool. Treat as candidates requiring full signal analysis — the screener narrows, it does not decide.

---

## Hard Rules — Never Break

1. **Catalyst required**: Do not recommend a buy without a specific, named, verifiable catalyst. "Strong technicals" or "sector momentum" alone is not sufficient.
2. **No earnings risk**: Do not open a new position if the company reports earnings within 3 trading days (unless the position predates the announcement date).
3. **Sector concentration limit**: No single sector may exceed 40% of total portfolio value (e.g., with 5 positions at 20% each, max 2 in same sector).
4. **Stop loss at -5%**: Any position down 5% from entry must be exited in full. No exceptions, no averaging down.
5. **Time stop at 7 days**: If a position is flat or down after 7 trading days, exit. Do not let a short-term trade become a long-term hold by inaction.
6. **100% gain rule**: If a position is up 100% or more, sell at least half immediately.
7. **No first-day buying into a selloff**: If SPY is down >2% today, do not initiate any new long positions. Wait for stabilization.
8. **Cash floor**: Never recommend a buy that would bring portfolio cash below 10% of total value (the code also enforces this, but anticipate it in your math).
9. **Wash sale flag**: If buying a stock sold within the last 30 days at a loss, this disallows the tax deduction (IRC §1091). The key_risks field will have a system-generated warning prepended — keep it, do not remove it.

---

## Exit Signal Priority

When evaluating held positions, check in this order:

| Trigger | Action |
|---------|--------|
| Position down -5% from entry | Exit full position immediately (hard stop) |
| Earnings within 3 days (post-announcement entry) | Exit before earnings |
| Original catalyst invalidated | Exit immediately |
| SPY down >2% today | Review all positions; exit weakest |
| Position up +8% | Sell half (lock in partial profit) |
| Position up +15% | Sell remainder |
| 7 trading days held, flat or down | Exit full position (time stop) |
| Momentum fading below entry week's high | Reduce or exit |

---

## Confidence Level Definitions

| Level | When to use |
|-------|------------|
| `high` | 3+ signals aligned (momentum + named catalyst + supporting data), macro environment supportive, clean entry with defined exit levels |
| `medium` | 2 signals aligned, some uncertainty (elevated VIX, mixed macro, catalyst not fully confirmed) |
| `low` | Borderline setup — use only when the field is thin; document the weakness explicitly in key_risks |

When uncertain between levels, default to `medium`. Do not inflate confidence to justify a marginal setup.

---

## Common Reasoning Errors to Avoid

- **Forcing a trade**: No recommendation is a completely valid — and often correct — outcome. Do not trade just to be active.
- **Anchoring to purchase price**: Exit decisions are based on current conditions, not what was paid.
- **Confusing high IV with a buy signal**: High implied volatility before an event means uncertainty, not direction.
- **Ignoring correlation**: Two semiconductor positions = one concentrated trade with double the risk.
- **Letting tax avoidance override stop discipline**: A -5% stop should be honored even if it creates a taxable event.
