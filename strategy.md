# Trading Strategy

This file tells the trading bot what YOU want it to do. Edit the answers below.
The bot reads this every morning before making recommendations.
Leave a section blank if you have no preference — the bot will use its own judgment.

---

## Trading Style

What is your primary trading approach?

Short-term momentum trading. Buy stocks that are already moving up strongly this week on
high volume with a clear catalyst. Do not look for undervalued or beaten-down stocks —
look for stocks showing price strength and near-term tailwinds. This is not a buy-and-hold
portfolio. Every position should have a clear exit plan before entry.

---

## Time Horizon

How long do you typically want to hold a position?

3 to 10 trading days. Rarely hold longer than two weeks. If a position is not working
within a week, exit regardless of thesis. Do not hold through extended drawdowns hoping
for a recovery.

---

## Entry Criteria

What conditions should trigger a buy recommendation?

- Stock is up 3–8% over the past 5 trading days with accelerating volume
- There is a specific, named catalyst (earnings beat, analyst upgrade, product launch,
  sector rotation into this stock's area)
- Stock is not overextended — ideally pulling back slightly from a recent high, not in
  a parabolic spike that is likely to reverse
- Broader market is not in a sharp selloff (do not buy into a falling market)

---

## Exit Criteria

What conditions should trigger a sell recommendation?

- Profit target: consider selling 50% at +8% gain, sell remainder at +15% or if
  momentum fades below the entry week's high
- Stop loss: exit the full position if it falls 5% from entry price
- Time stop: if a position is flat or down after 7 trading days, exit regardless of thesis
- If the original catalyst is no longer valid, exit immediately
- Never let a short-term trade become a long-term hold by default

---

## Risk Tolerance

How aggressive should position sizing be? What is your maximum drawdown tolerance?

Moderate-aggressive. Aim for 15–25% of portfolio per position. Maximum 3 simultaneous
positions. Keep at least 15% in cash at all times as dry powder for new opportunities.
If the overall portfolio is down more than 12% from starting capital, reduce new position
sizes by half until recovery.

---

## Sector Preferences

Which sectors or themes should the bot favor or avoid?

Prefer: Technology (software, semiconductors, AI infrastructure), Healthcare (biotech
with near-term catalysts), Consumer discretionary during strong macro environments.
These sectors have the strongest momentum characteristics and clear catalysts.

Avoid: Utilities, REITs, Consumer staples. These are low-volatility, dividend-oriented
sectors that do not produce the short-term momentum setups this strategy targets.

---

## Market Condition Rules

When should the bot be more aggressive or pull back?

Be more aggressive when VIX is below 20 and SPY is in an uptrend (above its 20-day
moving average). Pull back or go to cash when VIX spikes above 30, when SPY drops more
than 2% in a single day, or when three consecutive trading days produce no good setups —
do not force trades just to be active.

---

## Special Rules

Any hard rules the bot should never break?

- Do not hold a position through an upcoming earnings report if the position was opened
  after the earnings date was announced — the risk is binary and unpredictable
- No more than one position in the same sector at the same time
- If a position has gained 100% or more, sell at least half immediately to take risk off
- Do not buy on the first day of a sharp market-wide selloff — wait for stabilization
