# Scorched — Backlog

Deferred work items — captured here so they don't get lost in chat history. Ship in priority order when buy-rate data confirms the item still matters.

---

## BL-1 — Mean-reversion screener

**Problem.** `strategy.json → entry_style` declares `["breakout", "mean_reversion"]`, but the Phase 0 screener (`_fetch_momentum_screener_sync` in `src/scorched/services/research.py`) only surfaces top S&P-500 5-day-momentum names — all breakout candidates by construction. Mean-reversion candidates (RSI 25–40, %B ≤ 0, rising 50-day MA, 5–15% off 52-week high) are mechanically absent from Claude's candidate pool, so the declared second entry style never fires.

**Proposal.** Add a second screener pass that pulls S&P-500 names matching:
- Rising 50-day MA (confirmed larger uptrend)
- RSI(14) between 25 and 40
- Price 5–15% below 52-week high
- Volume within normal range (0.5–1.5× avg)

Surface up to 10 additional candidates; tag each with `entry_profile: "mean_reversion"` in the Phase 0 cache so `build_research_context` can render them separately and Claude applies the mean-reversion rules (no volume confirmation required) instead of the breakout rules.

**Where the change lives.**
- `src/scorched/services/research.py` — new `_fetch_mean_reversion_screener_sync()` + merge into Phase 0 gather
- `src/scorched/api/prefetch.py` — include the new screener output in the cache payload
- `src/scorched/services/recommender.py` / `build_research_context` — render tagged candidates in a distinct section

**Acceptance.** Phase 1 context shows both a MOMENTUM_CANDIDATES section and a MEAN_REVERSION_CANDIDATES section. On at least two sessions in a row where the momentum side produces zero buys, the mean-reversion side produces at least one qualified candidate.

**Dependencies.** None — can ship after the volume-tier prompt patch (already shipped 2026-04-24) shows whether loosening breakout criteria alone resolves buy-rate.

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
