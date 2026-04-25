# Scorched Trading Bot — Full Technical & Risk Audit

Prepared for: Will Cassell  
Repository: https://github.com/willcassell/scorched  
Local checkout: `/home/ubuntu/trading-audits/scorched`  
Report generated: 2026-04-25 10:05 EDT  
Reviewer: Hermes

---

## Executive Summary

Scorched is a thoughtful, unusually well-documented trading-bot project. The repository shows clear intent around disciplined swing/position trading, multi-source research, Claude-assisted analysis, paper/live broker adapters, operational cron phases, risk review, and post-trade learning. The design is not just a toy script: it has meaningful architecture, persistence, reconciliation, API-call tracking, drawdown gates, sector exposure checks, and extensive strategy guidance.

That said, the current implementation is not yet safe enough for unattended live trading. The biggest theme is that several rules described as “hard rules” are still partly prompt-enforced, operationally fail-open, or only checked during recommendation generation — not again at final order confirmation. The most important risks are:

1. Trade confirmation can submit arbitrary size/price from the request body without revalidating against the stored recommendation or risk gates.
2. Authentication is optional/fail-open unless `SETTINGS_PIN` is configured, and several sensitive read endpoints are intentionally unauthenticated.
3. Cash, max-holdings, and max-position gates have important edge cases that allow portfolio-rule breaches.
4. Stop-loss behavior is internally inconsistent: docs say -8%, config/code effectively use -5% in places, and hard-stop limit sells may fail in fast markets.
5. Circuit-breaker safety checks rely on yfinance daily history rather than the project’s primary live Alpaca snapshot feed.
6. Phase 0/Phase 1 data-source failure handling is less graceful than the docs imply.
7. There is no real historical backtesting/walk-forward evaluation harness, so the project validates workflow more than trading edge.

My overall recommendation: keep running this in paper mode while hardening deterministic risk controls, authentication, and evaluation. Treat live mode as blocked until the critical/high findings below are addressed.

---

## Overall Assessment

| Area | Rating | Notes |
|---|---:|---|
| Product/design clarity | Strong | Clear docs, strategy, data-source rationale, and phase architecture. |
| Code organization | Good | Reasonable FastAPI/service/broker separation; some stale duplicate scripts remain. |
| Data pipeline | Moderate | Multi-source and cached, but failure/freshness semantics need hardening. |
| Trading/risk controls | Moderate | Many controls exist, but several hard rules are prompt-only or have implementation gaps. |
| Broker/live-trading safety | Needs work | Good reconciliation ideas, but final confirmation path and pending-order/cash handling are risky. |
| Security/auth | Needs work | Fail-open PIN model and unauthenticated sensitive read endpoints are not acceptable if exposed. |
| Testing | Moderate | Many tests pass with dummy env, but security tests fail and import-time settings are brittle. |
| Backtesting/evaluation | Weak | Paper workflow exists; historical edge validation is absent. |

---

## Scope & Methodology

I reviewed the repository directly and ran three parallel specialist passes:

- Code/data-flow/operational reliability review
- Strategy/risk/live-trading safety review
- Data quality/backtesting/security/deployment review

Key files reviewed included:

- `README.md`
- `DATA_SOURCES.md`
- `strategy.md`
- `strategy.json`
- `analyst_guidance.md`
- `DEPLOY.md`
- `docker-compose.yml`
- `cron/*.py`
- `src/scorched/services/recommender.py`
- `src/scorched/services/research.py`
- `src/scorched/services/claude_client.py`
- `src/scorched/api/*.py`
- `src/scorched/broker/*.py`
- `src/scorched/circuit_breaker.py`
- `src/scorched/trailing_stops.py`
- `src/scorched/intraday.py`
- `src/scorched/models.py`

Verification performed:

- Repository cloned/pulled from GitHub.
- Recent git history inspected.
- Code inventory and source review performed.
- Specialist audits performed independently.
- Test status from specialist run:
  - `python3 -m pytest -q` fails without `ANTHROPIC_API_KEY` because settings require it at import time.
  - `ANTHROPIC_API_KEY=test python3 -m pytest -q` produced `247 passed, 3 failed`.
  - The 3 failing tests were onboarding auth tests.

---

## Positive Findings

These are worth preserving:

- Strong documentation of strategy intent, data sources, and limitations.
- Alpaca has replaced yfinance for primary daily price bars/snapshots in the main research path.
- Today’s in-progress Alpaca daily bar is stripped in the main technical path before computing technicals.
- Claude call architecture is split into analysis, decision, and risk review, which is a sensible pattern.
- Risk-review parse failure fails closed for buy recommendations.
- Drawdown gate exists and filters buys when threshold is exceeded.
- Sector concentration, max position, max holdings, cash reserve, wash-sale warnings, and correlation warnings exist in code.
- Alpaca sell path checks actual broker position and caps sell quantity, reducing accidental short risk.
- Pending fills are written before Alpaca order submission, improving crash recovery.
- Reconciliation handles normal fills, terminal orders, partial fills, and some idempotency cases.
- API call tracking and system health aggregation exist.
- Startup migrations fail closed if Alembic fails.
- `.env` is ignored by git.
- Docker binds app to localhost and a private Tailscale IP by default rather than `0.0.0.0`.

---

# Critical Findings

## C1 — Trade confirmation can execute arbitrary size/price without revalidating recommendation risk gates

Severity: Critical  
Files:

- `src/scorched/schemas.py:43-46`
- `src/scorched/api/trades.py:36-50`
- `src/scorched/services/portfolio.py:180-186`

The trade-confirm endpoint accepts `recommendation_id`, `execution_price`, and `shares` from the request body and passes those values directly to the broker. It does not re-check that:

- `shares` is less than or equal to the stored recommendation quantity
- `execution_price` is within a reasonable band around the stored recommendation/current market
- max position size still holds
- max holdings still holds
- sector exposure still holds
- cash floor still holds
- drawdown gate still allows buys
- circuit breaker still allows buys

`apply_buy()` checks only absolute cash sufficiency, not the intended 10% cash floor.

Impact: A malformed cron payload, manual/API mistake, compromised PIN, or MCP/tooling bug can place a buy much larger than the approved recommendation and bypass the risk model. In live mode, this is a direct capital-risk issue.

Recommendation:

- Treat stored `TradeRecommendation` as the source of truth.
- Reject confirmation if requested shares exceed recommendation shares, except perhaps a tiny rounding tolerance.
- Reject buy confirmation if price has drifted beyond a configured tolerance.
- Re-run deterministic cash, position, sector, holdings, drawdown, and circuit-breaker checks immediately before broker submission.
- Query Alpaca account/buying power before live orders.
- Consider making `/trades/confirm` accept only `recommendation_id` and let the server decide quantity/limit price.

---

## C2 — Authentication fails open when `SETTINGS_PIN` is unset

Severity: Critical  
Files:

- `src/scorched/config.py:31-34`
- `src/scorched/api/deps.py:8-13`
- `src/scorched/mcp_tools.py:24-31`
- `src/scorched/main.py:26-43`
- `DEPLOY.md:281-296`

`SETTINGS_PIN` defaults to empty. `require_owner_pin()` is a no-op when the PIN is missing. MCP mutation tools behave similarly. Live broker mode refuses to boot without a sufficiently strong PIN, which is good, but paper/Alpaca paper modes remain mutation-open if the service is reachable.

Impact: If the service is reachable over Tailscale, localhost proxy, misconfigured bind, or future deployment, unauthenticated callers can mutate system state and potentially submit paper/Alpaca-paper orders. The pattern is also dangerous because it trains operators to accept fail-open security in a trading app.

Recommendation:

- Make missing `SETTINGS_PIN` a startup failure for any mode with mutation endpoints enabled.
- Require auth for paper mode too.
- Prefer proper bearer/session auth over a raw PIN header/parameter.
- Keep only a minimal unauthenticated `/healthz` endpoint.

---

## C3 — Onboarding can write `.env` and strategy config when auth is unset

Severity: Critical  
Files:

- `src/scorched/api/onboarding.py:178-190`
- `src/scorched/api/onboarding.py:271-304`
- `src/scorched/api/deps.py:8-13`

Onboarding save/validate routes are protected only by `require_owner_pin`, which fails open if `SETTINGS_PIN` is empty. The save route can write `.env` and strategy settings.

Impact: An unauthenticated caller could alter API keys, broker mode, starting capital, strategy parameters, and deployment configuration if the app is exposed during or after setup.

Recommendation:

- Require a local-only bootstrap token for onboarding.
- Disable onboarding after initial setup unless explicitly re-enabled.
- Require `SETTINGS_PIN` or stronger auth before exposing onboarding routes.
- Consider binding onboarding only to localhost.

---

# High-Severity Findings

## H1 — Cash-floor enforcement is wrong and not cumulative across same-session buys

Files:

- `analyst_guidance.md:150`
- `src/scorched/services/recommender.py:829-839`

The docs say the portfolio must keep cash above 10% of total portfolio value. The code computes:

```python
min_cash = portfolio.cash_balance * settings.min_cash_reserve_pct
```

That uses current cash, not total portfolio value. The loop also does not decrement a running cash balance after each accepted buy, so multiple buys can collectively breach the floor.

Impact: The bot can allow cash far below the intended 10% reserve, especially when mostly invested or when multiple buys are accepted in one run.

Recommendation:

- Compute `min_cash = total_portfolio_value * reserve_pct`.
- Maintain a running `available_cash_after_accepted_buys` during filtering.
- Include pending submitted buy notional not yet reconciled.
- Re-check cash before final broker submission.

---

## H2 — Max holdings gate is not cumulative

Files:

- `strategy.json:18`
- `src/scorched/services/recommender.py:855-858`

For every proposed buy, the code uses `current_count = len(current_positions)`. It does not increment the count for accepted buys earlier in the same recommendation run.

Impact: If current holdings are below the limit, multiple same-day buys can push the portfolio above `max_holdings`. The sector gate has a running accepted-buy update; holdings should too.

Recommendation:

- Track `projected_holdings_count` through the loop.
- Decide whether adds to existing positions count differently from new symbols.
- Re-check at confirmation time.

---

## H3 — Max position cap checks only new buy dollars, not post-trade total exposure

Files:

- `strategy.json:16`
- `src/scorched/services/recommender.py:840-844`

The code rejects a buy only if `estimated_cost > max_pos_dollars`. It does not add the proposed buy to any existing position value.

Impact: Adding to an existing position can push that symbol over the 33% cap while passing the gate. Conversely, the max-holdings logic may block adds when already at holdings limit, which may or may not be intended.

Recommendation:

- Compute post-trade symbol exposure: existing market value + proposed notional.
- Apply max position cap to post-trade exposure.
- Define whether adding to an existing holding is allowed at max holdings.

---

## H4 — Stop-loss configuration conflicts: docs say -8%, config/code use -5% in places

Files:

- `strategy.md:17`
- `analyst_guidance.md:146,165`
- `strategy.json:47`
- `src/scorched/api/intraday.py:32-39`
- `src/scorched/api/intraday.py:160-170`

Docs say the hard stop is -8% from entry. `strategy.json` includes `hard_stop_pct=5.0`, while intraday code loads `intraday_monitor.position_drop_from_entry_pct`, not the explicit `hard_stop_pct` field.

Impact: The bot may hard-exit at 5% while the stated strategy expects 8%, creating premature exits and making performance analysis misleading.

Recommendation:

- Decide whether the hard stop is 5% or 8%.
- Store it in one canonical strategy field.
- Use that field consistently in prompts, docs, intraday code, tests, and dashboard.

---

## H5 — Intraday hard-stop sells use a limit at current/fresh price with no sell buffer

Files:

- `src/scorched/api/intraday.py:73-89`
- `cron/tradebot_phase2.py:143-181`

Normal Phase 2 sells use a sell buffer, but intraday hard-stop exits submit a DAY limit sell at the current/fresh price. In a falling market, that limit can sit above the bid and fail.

Impact: A “hard stop” becomes a best-effort limit order and may not fill when it matters most.

Recommendation:

- Use a marketable limit for stop-loss exits, e.g. current bid/last minus a configured emergency buffer.
- In live mode, consider broker-native stop or stop-limit orders where appropriate.
- Escalate failed stop-loss sells immediately and retry without cooldown.

---

## H6 — Failed intraday sells can be suppressed by cooldown

Files:

- `cron/intraday_monitor.py:352-362`
- `src/scorched/api/intraday.py:171-174`

The intraday monitor records cooldown when triggers are detected, before it knows whether the attempted exit succeeded. If a sell fails, the action can revert to hold, but the cooldown can still suppress retries.

Impact: A failed hard-stop/trailing-stop exit may not be retried promptly, increasing loss risk.

Recommendation:

- Apply cooldown only after successful terminal action, or use a separate short retry cadence for failed exits.
- Never suppress hard-stop retries solely because a prior attempt failed.

---

## H7 — Phase 0 treats exceptions from per-source gather as successful results

Files:

- `src/scorched/api/prefetch.py:51-61`
- `src/scorched/api/prefetch.py:156-161`
- `src/scorched/api/prefetch.py:174-180`

`_gather_with_timeout()` uses `asyncio.gather(..., return_exceptions=True)`, but downstream code destructures the result without checking for Exception objects. A failed source can become `price_data`, `sector_returns`, etc.

Impact: One data-source failure can crash Phase 0 later, corrupt cache shape, or produce misleading downstream context.

Recommendation:

- Normalize every source result to `{data, status, error, fetched_at}`.
- Substitute safe defaults for non-critical sources.
- Fail closed for critical sources such as price/snapshot data.
- Alert on degraded runs.

---

## H8 — Recommendation generation is not concurrency-safe

Files:

- `src/scorched/services/recommender.py:349-363`
- `src/scorched/services/recommender.py:575-581`
- `src/scorched/services/recommender.py:958`

`generate_recommendations()` checks for an existing session, then later inserts a new session row. Two concurrent calls can both pass the existence check and race on the unique session date. Force deletion is also not serialized.

Impact: Cron overlap, manual retry, API double-click, or slow Phase 0/Phase 1 can waste expensive Claude calls, fail mid-run, or leave partial side effects.

Recommendation:

- Use a DB advisory lock keyed by session date.
- Or insert a session/lock row before external calls and handle conflicts.
- Prevent concurrent `force=True` and non-force runs.

---

## H9 — Phase 2 deletes recommendations even when confirmations fail

Files:

- `cron/tradebot_phase2.py:204-232`
- `cron/tradebot_phase2.py:283-286`

Per-trade HTTP errors are appended to the Telegram summary, but the recommendations file is deleted in the final cleanup path.

Impact: A transient server/API/broker error can lose that day’s recommendations and prevent automatic retry.

Recommendation:

- Delete the file only after all recommendations reach terminal success/rejection.
- If some fail, rewrite the file with the unconfirmed subset and alert.

---

# Medium-Severity Findings

## M1 — Sensitive read endpoints are unauthenticated

Files:

- `src/scorched/api/recommendations.py:26-54`
- `src/scorched/api/system.py:19-86`
- `src/scorched/mcp_tools.py:156-229`
- `DEPLOY.md:289-295`

Recommendation/session lists, analysis text, portfolio info, playbook content, system health, and recent API errors are exposed without auth. Even if “read-only,” this data is sensitive trading intelligence.

Recommendation: Require auth for all read endpoints except a minimal public healthcheck.

---

## M2 — Circuit breaker uses yfinance daily history instead of Alpaca snapshots

Files:

- `src/scorched/circuit_breaker.py:117-146`
- `src/scorched/circuit_breaker.py:149-201`
- `DATA_SOURCES.md:26-45`

The circuit breaker calls `yf.Ticker(...).history(period="5d")` and treats the last daily close as current. This can be stale or delayed at the 9:55 pre-execution point.

Recommendation: Use Alpaca snapshots for equities/ETFs and a reliable VIX source. Record timestamps and fail closed if gate data is stale.

---

## M3 — Gap-up gate is implemented but unused

Files:

- `strategy.json:39`
- `src/scorched/circuit_breaker.py:97-114`
- `src/scorched/circuit_breaker.py:192-199`

`stock_gap_up_pct` exists in config and `check_gap_up_gate()` exists, but `run_circuit_breaker()` never calls it.

Impact: The chase-risk blocker is dead code.

Recommendation: Call `check_gap_up_gate()` for buys and combine it with stock/market gate results.

---

## M4 — Several “hard rules” are prompt-enforced rather than deterministic

Files:

- `analyst_guidance.md:141-151`
- `src/scorched/services/claude_client.py:77-104`
- `src/scorched/services/recommender.py:813-899`

Rules such as named catalyst required, no earnings within 3 trading days, factor alignment, 30-day time stop, and 100% gain partial sell are largely left to Claude’s prompt compliance.

Recommendation: Add deterministic post-LLM validators for each hard rule. Prompt guidance is useful, but hard rules should be code.

---

## M5 — Data freshness/lookahead semantics are inconsistent across bar-derived paths

Files:

- Good mitigation: `src/scorched/services/research.py:68-75`
- Remaining risks: `src/scorched/services/research.py:823-845`, `1557-1575`, `1584-1609`

The main technical path strips today’s in-progress daily bar, but momentum screener, sector returns, and factor returns appear to use `bars[-1]` directly.

Impact: Some signals may use in-progress bars while others use completed sessions plus snapshots. That makes strategy behavior harder to reason about and harder to backtest point-in-time.

Recommendation: Centralize bar hygiene and require every bar-derived function to declare completed-only vs intraday-aware behavior.

---

## M6 — Inline Phase 1 data fetch can abort on any single source failure

Files:

- `src/scorched/services/recommender.py:435-455`
- `src/scorched/services/research.py:1474-1485`

Phase 0 uses `return_exceptions=True` but mishandles it; inline Phase 1 uses `gather()` without exception isolation. The final context does not provide strong per-symbol freshness/missingness flags.

Recommendation: Use consistent source status normalization for both Phase 0 and Phase 1. Feed Claude structured data-quality indicators.

---

## M7 — REST and MCP trade confirmation diverge; MCP is broken for Alpaca submitted orders

Files:

- `src/scorched/api/trades.py:76-91`
- `src/scorched/mcp_tools.py:127-152`

REST handles Alpaca’s fire-and-forget `submitted` response by marking recommendation status as submitted. MCP expects `filled` and can report an error even when the order was submitted and a pending fill was written.

Impact: MCP can submit an Alpaca order while telling the operator it failed and leaving recommendation status stale.

Recommendation: Extract shared trade-confirm service logic used by both REST and MCP.

---

## M8 — Live order sizing ignores outstanding submitted buys

Files:

- `src/scorched/services/recommender.py:829-838`
- `src/scorched/broker/alpaca.py:107-137`
- `src/scorched/api/trades.py:76-79`

Alpaca fire-and-forget submissions do not immediately deduct local cash. Multiple submitted buys can be evaluated against the same stale local cash before reconciliation.

Recommendation: Include pending buy notional in cash/buying-power calculations or query Alpaca before each order.

---

## M9 — Broker reconciliation has transaction-boundary/idempotency gaps

Files:

- `src/scorched/services/portfolio.py:172-237`, `257-321`
- `src/scorched/broker/alpaca.py:417-428`, `438-448`, `472-491`

`apply_buy()` and `apply_sell()` commit internally, then reconciliation removes pending fills and commits again. This leaves a crash window. Full-fill idempotency guard exists, but terminal partial-fill handling lacks the same existing-trade guard.

Recommendation: Make trade application and pending-fill removal one transaction. Add partial-fill idempotency checks.

---

## M10 — Sector concentration gate allows unknown-sector names through

Files:

- `src/scorched/services/recommender.py:286-294`
- `src/scorched/services/recommender.py:869-889`

If a symbol has no sector metadata, the sector gate logs a warning and allows the buy.

Impact: An incomplete sector map can bypass a hard 40% sector cap.

Recommendation: Fail closed for unknown sectors, or use a reliable sector-classification fallback.

---

## M11 — Trailing stops are alert/evaluation triggers, not guaranteed exits

Files:

- `src/scorched/intraday.py:13-25`
- `src/scorched/intraday.py:153-156`
- `src/scorched/api/intraday.py:183-236`

A trailing-stop breach is sent into Claude review unless the hard entry-loss stop fires. Claude can choose hold.

Impact: This may be intended, but it conflicts with normal “stop” semantics and should be explicitly documented.

Recommendation: Decide whether trailing stops are mandatory exits or advisory alerts. If mandatory, make them deterministic.

---

## M12 — Tests currently require dummy secrets and onboarding auth tests fail

Files:

- `src/scorched/config.py:10,37`
- `tests/test_onboarding_auth.py:7-43`

The test suite requires `ANTHROPIC_API_KEY` at import time. With `ANTHROPIC_API_KEY=test`, 247 tests passed and 3 onboarding auth tests failed. Observed failures expected 403 but received 200/422.

Recommendation:

- Make settings injectable via dependency or `get_settings()`.
- Provide safe test defaults.
- Fix onboarding auth tests and require them in CI.

---

## M13 — Backtesting and walk-forward evaluation are absent

Files:

- `CONTRIBUTING.md:60-63`
- `src/scorched/services/research.py:1162-1224`
- `src/scorched/services/recommender.py:518-535`
- `pyproject.toml:29-34`

The repo explicitly excludes a backtesting engine. Paper trading validates plumbing, but it does not establish historical edge or risk-adjusted performance.

Recommendation: Build a point-in-time evaluation harness with frozen historical snapshots, slippage assumptions, benchmark comparisons, source ablations, and confidence calibration.

---

# Lower-Severity / Cleanup Findings

## L1 — Stale duplicate phase scripts remain under `src/`

Files:

- `src/tradebot_phase1.py`
- `src/tradebot_phase2.py`
- `src/tradebot_phase3.py`

These appear stale relative to `cron/` scripts and contain obsolete paths/payloads/redacted-looking code.

Recommendation: Delete, quarantine, or clearly mark obsolete. Keep one canonical cron implementation.

---

## L2 — Polygon references remain after Polygon removal

Files:

- `DATA_SOURCES.md:5`
- `src/scorched/api/onboarding.py:23,86-95,167-174,228-233`

Docs say Polygon was removed, but onboarding still whitelists/validates `POLYGON_API_KEY`.

Recommendation: Remove Polygon from onboarding, validators, env templates, and tests unless kept as clearly marked legacy migration support.

---

## L3 — Docker Compose hard-codes a specific Tailscale IP

File:

- `docker-compose.yml:19-22`

The app binds to a specific `100.77.184.61` address. That is brittle outside the current host.

Recommendation: Make the bind IP an env var or profile. Default to localhost-only.

---

## L4 — Cache/log directory is hard-coded to `/app/logs`

Files:

- `src/scorched/api/prefetch.py:44,244-250`
- `src/scorched/services/recommender.py:52-59`

Works in Docker, but local non-Docker runs may fail unless `/app/logs` exists.

Recommendation: Move cache/log path into settings with Docker and local defaults.

---

## L5 — Broker health is hardcoded green

Files:

- `src/scorched/api/system.py:19-59`
- `src/scorched/api/system.py:50-53`

System health reports broker status without checking Alpaca account/order API health.

Recommendation: Add real broker health checks: account status, buying power, live/paper mode, credential validity, pending-fill age, recent order errors.

---

## L6 — System error endpoint leaks operational details

Files:

- `src/scorched/api/system.py:62-86`
- `src/scorched/api_tracker.py:32-44`

Redaction exists, but unauthenticated API error details can still leak provider names, symbols, endpoints, timing, and operational behavior.

Recommendation: Authenticate system endpoints and expose only minimal public health.

---

# Recommended Remediation Plan

## Phase 1 — Live-trading safety blockers

Address before any unattended live deployment:

1. Lock down auth:
   - Fail startup if `SETTINGS_PIN`/auth is missing.
   - Auth all mutation and sensitive read endpoints.
   - Disable unauthenticated onboarding.
2. Fix `/trades/confirm`:
   - Use stored recommendation as source of truth.
   - Cap quantity/price drift.
   - Re-run all risk gates at confirmation.
3. Fix cash/holdings/position gates:
   - Use total value for cash floor.
   - Track cumulative accepted buys.
   - Include pending buy notional.
   - Apply max-position cap to post-trade exposure.
4. Resolve stop-loss semantics:
   - Pick 5% or 8% and make it canonical.
   - Make stop-loss exits marketable and retry hard-stop failures.
5. Use Alpaca snapshots for circuit breaker gate data.
6. Make hard rules deterministic for at least:
   - earnings blackout
   - no named catalyst
   - SPY/VIX market block
   - max exposure/concentration
   - 100% gain partial sell
   - 30-day time stop

## Phase 2 — Operational hardening

1. Normalize data-source results with freshness/error metadata.
2. Add a DB/session lock around recommendation generation.
3. Retain failed Phase 2 recommendation files for retry.
4. Unify REST and MCP trade confirmation logic.
5. Make reconciliation atomic and idempotent for partial fills.
6. Add real broker health checks.
7. Remove stale phase scripts and Polygon leftovers.
8. Move cache/log directory and bind IPs to settings.

## Phase 3 — Strategy validation

1. Build a point-in-time backtest/walk-forward harness.
2. Freeze historical data snapshots by decision timestamp.
3. Model slippage, limit-fill probability, commissions/fees if relevant.
4. Compare against SPY/QQQ/IWM and simple momentum baselines.
5. Track recommendation confidence calibration.
6. Run ablations:
   - without Claude risk review
   - without analyst consensus
   - without options data
   - without macro filters
   - with deterministic-only rules

---

# Unanswered Questions

After reviewing the repo, these are the main questions that still need Will/operator decisions:

1. Is the intended hard stop 5% or 8%?
2. Should trailing-stop breaches be mandatory sells or Claude-reviewed alerts?
3. Should unknown-sector candidates fail closed until sector metadata is available?
4. Should live mode require an explicit second kill switch such as `LIVE_TRADING_ENABLED=true`, separate from `BROKER_MODE=alpaca_live`?
5. Should `/trades/confirm` allow operator-edited size/price, or should it strictly execute the stored recommendation with only small tolerances?
6. Are adds to existing positions allowed when already at max holdings?
7. Should the max-position cap apply to post-trade total symbol exposure? I strongly recommend yes.
8. Should hard rules like earnings blackout, 30-day time stop, and 100% gain partial sell be deterministic validators? I strongly recommend yes.
9. Is the project intended to be private/Tailscale-only forever, or should it be hardened for normal internet exposure?
10. What is the minimum backtest/walk-forward evidence needed before live trading is acceptable?

---

# Bottom Line

Scorched has a solid foundation and a much better-than-average structure for an AI-assisted trading system. The project is directionally sound as a paper-trading research and workflow automation system.

It is not yet ready for unattended live trading. The blockers are fixable, but they are real: final order confirmation needs to be treated as a deterministic risk-control boundary, auth needs to be fail-closed, and the strategy’s “hard rules” need to be enforced by code rather than prompt compliance.

Recommended operating posture right now:

- Continue in paper mode.
- Do not enable `alpaca_live` until Critical and High findings are resolved.
- Prioritize deterministic risk enforcement and auth before adding new features.
- Add walk-forward/backtest evidence before trusting the strategy with meaningful capital.
