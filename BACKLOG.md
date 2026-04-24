# Scorched — Backlog

Deferred work items — captured here so they don't get lost in chat history. Ship in priority order when buy-rate data confirms the item still matters.

---

## BL-1 — Mean-reversion screener — SHIPPED 2026-04-24

Shipped in same commit as the tier-2 volume / factor-sector / macro-event prompt patch. See `project_mean_reversion_screener.md` in memory for details. Resolution criteria (at least one qualified mean-reversion candidate during a pullback-regime session within the next 10 sessions) is now the trigger for closing the parent buy-rate watch item.

Key notes for future tuning:
- Screener uses Alpaca IEX bars (same path as momentum screener). 180-day fetch covers RSI(14) + 50d MA + 21-bar slope lookback.
- Filter: price > 20d MA, 5–15% below 60d high, RSI 25–48, uptrend intact (price > 50d MA OR rising slope), 200K IEX-only avg volume (~8–10M consolidated).
- RSI range broadened from the traditional 25–40 to 25–48 so the screener still fires in strong-trend regimes where pullbacks rarely reach deep-oversold. Matching edit in `analyst_guidance.md` mean-reversion rule.
- On 2026-04-24 the screener returned 0 picks against a MTUM +8% regime — correct behavior, validated via `/tmp/mean_rev_diag.py` (closest miss was COF at RSI 48.5, one point above threshold).
- If later tuning shows too many false positives in 40–48 RSI range, tighten back to 25–45 and update guidance in lockstep.

---

## BL-2 — Expand Phase 1 pre-filter cap from 25 → 40 symbols

**Problem.** `build_research_context` (in `services/research.py`) scores all symbols and sends Claude the top 25 plus held positions. Scoring weights momentum + news catalyst + technical alignment + volume + relative strength + insider activity — so the prefilter already favors confirmed-breakout setups. Combined with Call 2's conservatism, this is a double filter that starves Claude of options on quieter days.

**Proposal.** Raise the prefilter cap to 40. Claude Call 1 has extended thinking (16 000 token budget) and can narrow further — it's better at catalyst-quality judgment than a linear score. Measure cost impact: 40 symbols × the per-symbol context size will grow Call 1 input tokens modestly (~30–40 %); cost increase likely under $0.05/day.

**Where the change lives.**
- `src/scorched/services/research.py` — constant near `build_research_context`

**Acceptance.** Phase 1 context size stays under 50 000 chars (was ~36 000 at 25-symbol cap on 2026-04-24). Per-session cost stays under $0.20. Buy rate over 10 sessions improves vs. the 25-cap baseline by at least 1 additional qualifying candidate surfaced per week.

**Dependencies.** Ship only after BL-1 if the prompt patch + BL-1 together don't raise buy-rate to target (3-of-5 sessions producing ≥1 buy). Tuning a parameter before the structural fix is the wrong order.

---

## BL-3 — Prompt audit for short-horizon bias (diagnostic)

**Problem.** Since 2026-04-21 the daily playbook update has been **drift-rejected** every session for the same pattern ("10-day time ceiling, 3-10 day holding window"). The guardrail (`_DRIFT_PATTERNS` in `services/playbook.py`) is working, but Claude keeps *trying* to install shorter-hold rules than the declared 2–6 week window. That's a signal: something in `analysis.md` or `decision.md` is cumulatively nudging Claude toward a day-trader mindset, and the playbook is where the nudge surfaces.

**Proposal.** Single-pass audit of the runtime prompts for short-horizon language:
1. `src/scorched/prompts/analysis.md`
2. `src/scorched/prompts/decision.md`
3. `analyst_guidance.md`
4. `src/scorched/prompts/position_mgmt.md`
5. `src/scorched/prompts/intraday_exit.md`

Look for: heavy use of "momentum", "chasing", "parabolic", "breakout confirmation" language that — while individually reasonable — cumulatively pushes toward a "need a clean breakout print today" mindset. Compare with the explicit 2–6 week horizon declared in `strategy.json` and hard rule #5 (30-day time stop).

**Deliverable.** A markdown diagnosis noting any specific passages to rewrite with a 2–6-week-position-trader framing. No code changes in this ticket — ship as a follow-up PR with targeted rewrites.

**Dependencies.** Do this last. If BL-1 + the 2026-04-24 prompt patch restore buy rate, the short-horizon nudge may already be resolved by explicit catalyst-tier + macro-event guidance.

---

## Notes

- Track against the Buy-rate watch item in `~/.claude/projects/-home-ubuntu-tradebot/memory/project_buy_rate_watchlist.md` — resolution criteria there govern when each BL-item becomes urgent vs. can stay deferred.
- Before starting BL-1, verify `strategy.md` vs `strategy.json` — `strategy.md:15` says "Max positions: 5 simultaneous" but `strategy.json → concentration.max_holdings = 10`. Reconcile in the same change.
