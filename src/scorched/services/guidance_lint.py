#!/usr/bin/env python3
"""Guidance linter — verify analyst_guidance.md stays in sync with strategy.json.

Exits 0 when everything aligns, 1 when any finding has severity "error". Warnings
are informational and do not fail the check. Designed to run in CI, at bot
startup, and on demand from the dashboard's /api/v1/guidance/lint endpoint.

The checks are deliberately narrow — we only lint numbers that exist in BOTH
files. Rules that live only in analyst_guidance.md (e.g. rule #5's 30-day time
stop) surface as info-level "prompt-only, no code equivalent" rows so the panel
can flag them as candidates for future promotion to strategy.json.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Literal


Severity = Literal["ok", "info", "warning", "error"]


@dataclass
class Finding:
    rule_number: int | None
    check: str
    severity: Severity
    message: str
    # Optional fields for the panel to render side-by-side.
    strategy_value: str | None = None
    guidance_value: str | None = None


def _find_first(pattern: str, text: str, flags: int = 0) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(1) if m else None


def _check_sector_limit(strategy: dict, guidance: str) -> Finding:
    strat_val = (strategy.get("concentration") or {}).get("max_sector_pct")
    # Match rule 3 wording "may exceed 40% of total portfolio value".
    guide_val = _find_first(
        r"No single sector may exceed\s+(\d+)\s*%", guidance, re.IGNORECASE,
    )
    if strat_val is None or guide_val is None:
        return Finding(
            rule_number=3, check="sector_limit", severity="warning",
            message="Could not locate sector limit in one of the files",
            strategy_value=str(strat_val), guidance_value=guide_val,
        )
    if int(guide_val) != int(strat_val):
        return Finding(
            rule_number=3, check="sector_limit", severity="error",
            message=f"Sector limit mismatch: strategy.json says {strat_val}%, "
                    f"analyst_guidance.md says {guide_val}%",
            strategy_value=f"{strat_val}%", guidance_value=f"{guide_val}%",
        )
    return Finding(
        rule_number=3, check="sector_limit", severity="ok",
        message="Sector limit: both files agree",
        strategy_value=f"{strat_val}%", guidance_value=f"{guide_val}%",
    )


def _check_stop_loss(strategy: dict, guidance: str) -> Finding:
    """Verify hard stop is 8% in both strategy.json and analyst_guidance.md rule #4."""
    strat_val = (strategy.get("intraday_monitor") or {}).get("hard_stop_pct")
    guide_val = _find_first(
        r"Stop loss at\s*-?(\d+(?:\.\d+)?)\s*%\s*from entry", guidance, re.IGNORECASE,
    )
    if strat_val is None or guide_val is None:
        return Finding(
            rule_number=4, check="hard_stop", severity="error",
            message="hard_stop_pct missing from strategy.json or guidance file",
            strategy_value=str(strat_val), guidance_value=guide_val,
        )
    if abs(float(strat_val) - float(guide_val)) > 1e-6:
        return Finding(
            rule_number=4, check="hard_stop", severity="error",
            message=f"Hard-stop mismatch: strategy.json hard_stop_pct={strat_val}%, "
                    f"guidance rule #4={guide_val}%. These MUST match.",
            strategy_value=f"{strat_val}%", guidance_value=f"{guide_val}%",
        )
    return Finding(
        rule_number=4, check="hard_stop", severity="ok",
        message="Hard stop: strategy.json and guidance agree",
        strategy_value=f"{strat_val}%", guidance_value=f"{guide_val}%",
    )


def _check_cash_floor(strategy: dict, guidance: str) -> Finding:
    # Rule 8: "Never recommend a buy that would bring portfolio cash below 10%"
    # strategy.json has no explicit cash_floor field today — it's hard-coded in
    # recommender.py. If a future version adds it, we'll wire this up.
    guide_val = _find_first(
        r"bring portfolio cash below\s+(\d+)\s*%", guidance, re.IGNORECASE,
    )
    if guide_val is None:
        return Finding(
            rule_number=8, check="cash_floor", severity="warning",
            message="Cash-floor percentage not located in guidance file",
        )
    # Probe commonly-used keys; if absent, report as prompt-only.
    for path in (("cash_floor_pct",), ("concentration", "min_cash_pct")):
        cursor = strategy
        for k in path:
            cursor = (cursor or {}).get(k)
            if cursor is None:
                break
        if cursor is not None:
            if int(guide_val) != int(cursor):
                return Finding(
                    rule_number=8, check="cash_floor", severity="error",
                    message=f"Cash floor mismatch: strategy.json={cursor}%, guidance={guide_val}%",
                    strategy_value=f"{cursor}%", guidance_value=f"{guide_val}%",
                )
            return Finding(
                rule_number=8, check="cash_floor", severity="ok",
                message="Cash floor: both files agree",
                strategy_value=f"{cursor}%", guidance_value=f"{guide_val}%",
            )
    return Finding(
        rule_number=8, check="cash_floor", severity="info",
        message="Cash floor is enforced in code (recommender.py) but has no "
                "strategy.json field today. Consider promoting to a toggle.",
        guidance_value=f"{guide_val}%",
    )


def _check_earnings_blackout(strategy: dict, guidance: str) -> Finding:
    # Rule 2: "within 3 trading days" — matches the first "within N trading
    # days" reference inside the Hard Rules block.
    guide_val = _find_first(
        r"earnings within\s+(\d+)\s+trading days", guidance, re.IGNORECASE,
    )
    if guide_val is None:
        return Finding(
            rule_number=2, check="earnings_blackout", severity="warning",
            message="Earnings blackout window not located in guidance",
        )
    override = (strategy.get("rule_overrides") or {}).get("earnings_blackout") or {}
    strat_days = override.get("days")
    strat_enabled = override.get("enabled", True)
    if not strat_enabled:
        return Finding(
            rule_number=2, check="earnings_blackout", severity="info",
            message="Earnings blackout is DISABLED via strategy.json override. "
                    "Guidance text still says 3 days — Claude will ignore it.",
            guidance_value=f"{guide_val} days",
            strategy_value="disabled",
        )
    if strat_days is None:
        return Finding(
            rule_number=2, check="earnings_blackout", severity="info",
            message="No override set; guidance value applies.",
            guidance_value=f"{guide_val} days",
            strategy_value=f"{guide_val} days (default)",
        )
    if int(guide_val) != int(strat_days):
        return Finding(
            rule_number=2, check="earnings_blackout", severity="error",
            message=f"Earnings blackout mismatch: override says {strat_days} "
                    f"days, guidance says {guide_val} days",
            guidance_value=f"{guide_val} days",
            strategy_value=f"{strat_days} days",
        )
    return Finding(
        rule_number=2, check="earnings_blackout", severity="ok",
        message="Earnings blackout window: override and guidance agree",
        guidance_value=f"{guide_val} days",
        strategy_value=f"{strat_days} days",
    )


def _check_selloff_threshold(strategy: dict, guidance: str) -> Finding:
    # Rule 7: "If SPY is down >2% today".
    guide_val = _find_first(
        r"SPY is down\s*>\s*(\d+(?:\.\d+)?)\s*%", guidance, re.IGNORECASE,
    )
    override = (strategy.get("rule_overrides") or {}).get("selloff_threshold") or {}
    strat_pct = override.get("spy_drop_pct")
    strat_enabled = override.get("enabled", True)
    if guide_val is None:
        return Finding(
            rule_number=7, check="selloff_threshold", severity="warning",
            message="SPY selloff threshold not located in guidance",
        )
    if not strat_enabled:
        return Finding(
            rule_number=7, check="selloff_threshold", severity="info",
            message="Rule #7 disabled via override. Guidance text still quotes "
                    ">2%; Claude will ignore it.",
            guidance_value=f">{guide_val}%", strategy_value="disabled",
        )
    if strat_pct is None:
        return Finding(
            rule_number=7, check="selloff_threshold", severity="ok",
            message="No override set; guidance value applies",
            guidance_value=f">{guide_val}%",
            strategy_value=f">{guide_val}% (default)",
        )
    if abs(float(guide_val) - float(strat_pct)) > 1e-6:
        return Finding(
            rule_number=7, check="selloff_threshold", severity="error",
            message=f"Selloff threshold mismatch: override={strat_pct}%, guidance={guide_val}%",
            guidance_value=f">{guide_val}%", strategy_value=f">{strat_pct}%",
        )
    return Finding(
        rule_number=7, check="selloff_threshold", severity="ok",
        message="Selloff threshold: override and guidance agree",
        guidance_value=f">{guide_val}%", strategy_value=f">{strat_pct}%",
    )


def _check_gain_trigger(strategy: dict, guidance: str) -> Finding:
    # Rule 6: "If a position is up 100% or more, sell at least half"
    guide_val = _find_first(
        r"position is up\s+(\d+)\s*%?\s+or more", guidance, re.IGNORECASE,
    )
    override = (strategy.get("rule_overrides") or {}).get("gain_trigger") or {}
    strat_pct = override.get("threshold_pct")
    strat_enabled = override.get("enabled", True)
    if guide_val is None:
        return Finding(
            rule_number=6, check="gain_trigger", severity="warning",
            message="100% gain trigger not located in guidance",
        )
    if not strat_enabled:
        return Finding(
            rule_number=6, check="gain_trigger", severity="info",
            message="Rule #6 disabled via override.",
            guidance_value=f"{guide_val}%", strategy_value="disabled",
        )
    if strat_pct is None:
        return Finding(
            rule_number=6, check="gain_trigger", severity="ok",
            message="No override set; guidance value applies",
            guidance_value=f"{guide_val}%",
            strategy_value=f"{guide_val}% (default)",
        )
    if int(guide_val) != int(strat_pct):
        return Finding(
            rule_number=6, check="gain_trigger", severity="error",
            message=f"Gain trigger mismatch: override={strat_pct}%, guidance={guide_val}%",
            guidance_value=f"{guide_val}%", strategy_value=f"{strat_pct}%",
        )
    return Finding(
        rule_number=6, check="gain_trigger", severity="ok",
        message="Gain trigger: override and guidance agree",
        guidance_value=f"{guide_val}%", strategy_value=f"{strat_pct}%",
    )


_CHECKS: tuple = (
    _check_sector_limit,
    _check_stop_loss,
    _check_cash_floor,
    _check_earnings_blackout,
    _check_selloff_threshold,
    _check_gain_trigger,
)


def lint(strategy_json: dict, guidance_text: str) -> list[Finding]:
    """Run every check and return ordered findings (by rule number)."""
    findings = [check(strategy_json, guidance_text) for check in _CHECKS]
    findings.sort(key=lambda f: (f.rule_number or 0, f.check))
    return findings


def summarize(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {"ok": 0, "info": 0, "warning": 0, "error": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def main() -> int:
    # Running as a module (`python3 -m scorched.services.guidance_lint`)
    # works from anywhere because the package is pip-installed in the
    # container. The strategy loader handles Docker volume mounts on its own.
    from .strategy import load_strategy_json
    from .guidance import load_guidance_with_meta

    strategy = load_strategy_json()
    meta = load_guidance_with_meta()
    findings = lint(strategy, meta.content)

    counts = summarize(findings)
    header = (
        f"Guidance linter — {counts['ok']} ok · {counts['info']} info · "
        f"{counts['warning']} warn · {counts['error']} err"
    )
    print(header)
    print("-" * len(header))
    for f in findings:
        tag = {"ok": "OK  ", "info": "INFO", "warning": "WARN", "error": "ERR "}[f.severity]
        rule = f"#{f.rule_number}" if f.rule_number else "  "
        print(f"  [{tag}] rule {rule} {f.check}: {f.message}")
        if f.strategy_value or f.guidance_value:
            print(f"              strategy={f.strategy_value!r:25s}  guidance={f.guidance_value!r}")
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
