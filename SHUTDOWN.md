# Scorched / Tradebot — Shutdown

**Date:** 2026-08-31 (Monday, ~08:15 ET, pre-market)
**Action:** Full stand-down. Experiment C retired ~8 weeks ahead of its 2026-10-27 deadline, by owner decision.
**Reversible:** Yes. Nothing was deleted. See "Restart" below.

---

## Why

`scripts/evaluate_experiment.py` returned **RETIRE** on both kill criteria:

```
Window since 2026-08-03:
  Portfolio: +0.24%  ($101,798.53 -> $102,038.15)
  SPY:       +2.79%  ($746.79 -> $767.66)
  Beats SPY: NO
  Profit factor: 0.36  (3 sells, wins $294 / losses $807)
  PF > 1.0:  NO
DECISION: RETIRE
```

The full-life record is the same story over a longer window: the bot returned roughly
+2% against an S&P up ~11% and a momentum factor up ~17-22%. It never demonstrated edge.
Experiment B reached the same verdict in June; C carried forward B's fixes plus mechanical
entry gating, re-entry cooldown, and exposure discipline, and still did not close the gap.

## Final numbers

Two windows, both true — do not conflate them.

**Alpaca paper account (broker of record, source of truth):**
- Funded once: $100,000.00 on 2026-03-30 (single JNLC; no later deposits or withdrawals)
- Final equity: **$101,948.63** -> **+1.95%**
- Composition: cash $73,777.07 + long market value $28,171.56; `short_market_value = 0`
- Lifetime fees: $1.70 (70 entries)

**Local DB, full run 2026-02-26 -> 2026-08-31:**
- Total value $102,038.15 on $100,000 starting capital -> **+2.04%**
- (Local vs broker differ by $89.52 — immaterial, live-quote timing.)

**Benchmarks, same 2/26 -> 8/31 window (start prices stored in `portfolio`):**

| Series | Start | End | Return |
|---|---|---|---|
| **Scorched** | 100,000.00 | 102,038.15 | **+2.04%** |
| SPY | 693.15 | 767.66 | +10.75% |
| QQQ | 616.68 | 714.41 | +15.85% |
| RSP | 202.7122 | 220.665 | +8.86% |
| MTUM | 256.1504 | 299.58 | +16.96% |
| SPMO | 120.1501 | 146.77 | +22.16% |

Underperformed every benchmark. Worst gap: **-20.1 pts vs SPMO**. vs SPY: **-8.7 pts**.

**Trade ledger (112 rows, 2026-02-26 -> 2026-08-19):**
- 49 buys / 63 sells (sells exceed buys: partial and multi-lot exits recorded per fill)
- 32 wins / 31 losses -> **50.8% win rate**
- Gross win $9,077.87 / gross loss $10,237.71 -> **profit factor 0.887**
- Realized P&L **-$1,159.84**; unrealized on open book **+$1,165.52**
- Zero NULL `realized_gain` rows — the ledger is complete, PF is not understated
- `exit_reason`: 60 NULL (pre-telemetry), 3 `intraday_claude_exit`. Telemetry shipped too late to be useful.

Monthly realized P&L: Feb +247.74 | Mar -2,892.00 | Apr +3,015.48 | May -1,452.83 | Jun -1,395.34 | Jul +1,829.71 | Aug -512.60

**Accounting note — a real $2,032 gap, stated not smoothed.** Trade-attributable P&L is
realized (-$1,159.84) + unrealized (+$1,165.52) = **+$5.68**, essentially flat. But the book
shows +$2,038.15. The difference traces to the 2026-04-18 cash reconciliation, which raised
local cash by $2,379.25 to match Alpaca after ghost-position drift from the pre-fire-and-forget
era. Read plainly: **the headline +2% is mostly a reconciliation correction, not trading skill.
Trading was flat-to-slightly-negative.** The honest verdict is worse than the headline.

**Unexplained, left open:** Alpaca's `/account/portfolio/history` daily series ends at
$90,085.62 for 2026-08-29 while the live account object reports $101,948.63. The live object
is internally consistent (cash + long MV = equity, no shorts) and the single $100k funding
journal corroborates it, so the holdings-based valuation is the one used above. The daily
series appears to be a paper-account reporting artifact. Not chased further — it does not
change the holdings math.

**LLM cost:** $31.79 across 762 calls, 2026-02-26 -> 2026-08-28.
Monthly: Feb $0.22 | Mar $2.86 | Apr $3.71 | May $3.60 | Jun $4.18 | Jul $3.72 | Aug $13.50
(August is high because of the 2026-07-31 all-Opus-5 migration plus the 8/05 fix that finally
priced thinking tokens at the output rate. Pre-8/05 `token_usage` rows are understated ~44%
on analysis calls and were never backfilled, so true lifetime spend is somewhat above $31.79.)

## Final open positions — LEFT OPEN, not liquidated

| Symbol | Shares | Avg cost | Last | Market value | Unrealized |
|---|---|---|---|---|---|
| MA | 24 | $578.11 | $595.215 | $14,236.56 | +$361.92 |
| PFE | 500 | $26.44 | $27.95 | $13,935.00 | +$715.00 |

Paper money, `live_trading_enabled = False`, so leaving them open carries no financial risk and
preserves the terminal state for the record. **They are now unmonitored** — trailing stops
(MA $581.82, PFE $27.37) will not fire, because the intraday monitor is off. The final figures
above are **mark-to-market as of 2026-08-31 pre-open, not a closed book.**

Pre-shutdown broker state was clean: `pending_fills.json` empty, zero open/resting orders,
no short exposure, local-vs-Alpaca cash drift $0.92 (under the $1.00 alert threshold).

## What was shut down

1. **Cron — all 9 tradebot jobs disabled** (Phase 0, 1, 1.5, 2, 2.5, 2.75, 3, intraday monitor,
   weekly reflection). Commented with a `#DISABLED ` prefix rather than deleted, under a dated
   header block. Installed via `crontab <file>`, never `crontab -e`.
   - Backup of live pre-shutdown crontab: `/home/ubuntu/crontab.backup.2026-08-31-shutdown`
   - Verified by diffing `crontab -l` against the backup: only the 9 intended lines changed.
   - Non-tradebot jobs deliberately untouched: nightly chezmoi sync (03:30), weekly Docker prune (Sun 04:00).
2. **Docker stack down** — `docker compose down` (no `-v`). Containers removed; the
   `postgres_data` and logs volumes are intact.
3. **Dashboard offline** — it was served by the FastAPI app itself on
   `100.77.184.61:8000` / `127.0.0.1:8000`. No `tailscale serve`/`funnel` entry pointed at it,
   so stopping the container fully removes it. No tunnel config needed cleanup.

Swept for other schedulers and found none: `/etc/cron.d/`, systemd system+user timers,
Claude Code `CronList`, and the OpenClaw / Hermes cron stores all have zero tradebot references.
Crontab really was the sole driver, as CLAUDE.md claimed.

## Archive — `/home/ubuntu/tradebot-archive/` (outside git, nothing committed)

| File | Contents |
|---|---|
| `scorched-db-2026-08-31.sql.gz` | Full `pg_dump` (1.4M gzipped) |
| `trade_history.csv` | 112 rows |
| `trade_recommendations.csv` | 105 rows |
| `token_usage.csv` | 762 rows |
| `equity_history.csv` | 16 rows (8/07-8/28 only — feature shipped late; **not** a six-month curve) |
| `positions.csv`, `portfolio.csv` | Terminal state |
| `logs-volume-2026-08-31.tar` | 138M — Phase 0 caches, pending fills, playbook rejections |
| `strategy-final.json` | Config as of retirement |
| `crontab.backup.2026-08-31-shutdown` | Pre-shutdown crontab |

## What actually killed it

Recorded so the next attempt doesn't relearn it:

- **Entries, not exits, were the problem.** The June backtest showed a mechanical breakout rule
  beating LLM-selected entries. C added the mechanical entry gate in response and still lost.
- **Chronic under-participation.** Long stretches of zero buys during strong tape (the 4/16-4/23
  window: 1 buy in 6 sessions while MTUM ran +6 pts). Prompt loosening on 4/24 helped some, not enough.
- **Gate stacking is a ratchet.** Seven BUY gates plus a default-reject risk committee meant the
  common failure was non-participation, and non-participation in a rising market is itself a loss.
  Cash sat at 72% of the book at the end.
- **~50% win rate with PF < 1.0** means losers were bigger than winners — the trailing-stop-only
  exit policy (no fixed profit targets, `partial_sell: never`) let winners round-trip.
- **Engineering was sound; the strategy was not.** Most of the six months went into pipeline
  reliability — reconciliation, idempotency, drift guards, telemetry — and those largely worked.
  None of it produced edge. Infrastructure quality was never the binding constraint.

## Volume-retention check (verified, no action needed)

The weekly Docker prune (`/home/ubuntu/bin/docker-weekly-prune.sh`, Sundays 04:00) is still
active and now runs against a stack whose volume is unreferenced — worth checking, because an
unused DB volume is exactly what a prune would eat.

It is safe. The script uses `docker volume prune -f`, not `-af`. On Docker 29.7.2 that removes
**anonymous volumes only**; `tradebot_postgres_data` is a *named* volume carrying
`com.docker.compose.*` labels and is not eligible. `image prune -f` likewise skips tagged
images, so the `tradebot-tradebot` image survives too.

Belt and braces regardless: the full `pg_dump` in `/home/ubuntu/tradebot-archive/` is outside
Docker entirely and does not depend on the volume surviving.

## Restart

Nothing is destroyed. To bring it back:

```bash
# 1. Re-enable cron (strip the disable prefix)
crontab -l > /tmp/cron.now
sed -i 's/^#DISABLED //' /tmp/cron.now
crontab /tmp/cron.now && crontab -l    # verify before trusting

# 2. Bring the stack up
docker compose up -d --build

# 3. Reconcile against the broker before letting it trade — the DB will be stale
curl -X POST -H "X-Owner-Pin: $PIN" http://127.0.0.1:8000/api/v1/broker/sync
```

**Before any restart, reset the experiment block in `strategy.json`** (`start_date`,
`deadline_approx_date`, baselines) or `evaluate_experiment.py` will keep grading against the
retired Experiment C window. And re-run `/broker/sync` first — MA and PFE will have drifted
arbitrarily far from their recorded stops while unmonitored.
