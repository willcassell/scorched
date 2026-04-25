#!/usr/bin/env python3
"""Strategy doc sync linter.

Scans the canonical strategy docs for phrases that match OLD strategy values —
phrasing that would indicate the doc has drifted away from strategy.json.

Exits 0 when clean, 1 when any forbidden pattern is found.

This is a pattern-based companion to scripts/guidance_lint.py:

  - guidance_lint.py is field-precise. It knows analyst_guidance.md rule #3's
    sector limit must equal strategy.json -> concentration.max_sector_pct, and
    parses each rule's text. Use it for the prompt Claude reads at runtime.

  - check_strategy_docs.py (this file) is pattern-based. It scans a wider doc
    set for forbidden phrases representing stale values. Cheaper, catches the
    "doc still describes last quarter's strategy" failure mode.

When strategy.json changes a numeric rule, append the OLD value's phrasing to
STALE_PATTERNS so a future regression to that value gets caught. The list
grows over time but never shrinks.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# Phrases that indicate a doc has drifted to OLD strategy values.
# Each entry: (regex pattern, human-readable description of what it caught)
# Patterns are case-insensitive. Tighten patterns to avoid false positives —
# e.g. "max_position_pct=20" is forbidden, but a fallback note like
# "current: 33; code default fallback: 20" is fine because letters separate
# the token from the number.
STALE_PATTERNS: list[tuple[str, str]] = [
    # max_holdings drift: was 5, now 10
    (r"\bmax\s*(?:imum)?\s*(?:simultaneous\s*)?positions?\s*[:|]?\s*5\b",
     "old max-positions=5"),
    (r"\bmax_holdings?\s*[:|=]\s*5\b", "old max_holdings=5"),
    (r"\b5\s*positions?\s*at\s*(?:~\s*)?20\s*%", "old 5×20% sizing math"),
    (r"\bmax\s*2\s*in\s*(?:any|same)\s*sector", "old max-2-per-sector math (assumed 5×20%)"),

    # max_position_pct drift: was 20, now 33
    (r"\bmax_position_pct\s*[:|=]\s*20\b", "old max_position_pct=20"),
    (r"15\s*[-–]\s*25\s*%\s*per\s*position", "old 15-25% sizing"),
    (r"position\s*siz(?:e|ing)[^.\n]{0,40}15\s*[-–]\s*25\s*%", "old 15-25% sizing prose"),

    # hold_period drift: was 3-10d, now 2-6wk
    (r"\b3\s*[-–]\s*10\s*(?:trading|calendar)?\s*days?\b", "old 3-10d hold period"),
    (r"\bhold_period\s*[:|=]\s*['\"]?3\s*[-–]\s*10d['\"]?", "old hold_period=3-10d"),

    # Old time/profit/stop rules (also in services/playbook.py _DRIFT_PATTERNS,
    # but we lint at commit time here, before bad values reach the prompt)
    (r"\b7\s*[-–]?\s*day\s+flat\b", "old 7-day flat-position rule"),
    (r"\+\s*8\s*%\s*partial", "old +8% partial-sell"),
    (r"\b10[-\s]?day\s+(?:ceiling|hard|maximum|time\s+stop|rule)\b",
     "old 10-day time ceiling"),
]


# Docs to lint. Keep this list narrow — only files that describe strategy
# values authoritatively. Skip:
#   - Prompt files (prompts/playbook_update.md) that mention old values as
#     anti-drift warnings.
#   - Project CLAUDE.md / src/scorched/CLAUDE.md ARE included only when they
#     describe strategy values; the project root CLAUDE.md is implementation
#     notes (architecture, gotchas) and references the drift guardrail by
#     listing the patterns it catches — those mentions are not drift.
DOCS_TO_LINT: list[str] = [
    "strategy.md",
    "analyst_guidance.md",
    "advisor.md",
    "SETUP_DEVELOPER.md",
    "src/scorched/CLAUDE.md",
    "README.md",
    "START_HERE.md",
]


def load_strategy() -> dict:
    return json.loads((ROOT / "strategy.json").read_text())


def lint_file(path: Path) -> list[str]:
    text = path.read_text()
    findings: list[str] = []
    for pattern, desc in STALE_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            line_no = text[: m.start()].count("\n") + 1
            line = text.splitlines()[line_no - 1].strip()
            findings.append(
                f"  {path.relative_to(ROOT)}:{line_no} — {desc}\n"
                f"      matched: {m.group(0)!r}\n"
                f"      line:    {line[:120]}{'…' if len(line) > 120 else ''}"
            )
    return findings


def main() -> int:
    strategy = load_strategy()
    conc = strategy.get("concentration", {})
    print("Strategy doc sync linter")
    print(
        f"  Current strategy.json: max_position_pct={conc.get('max_position_pct')}, "
        f"max_holdings={conc.get('max_holdings')}, "
        f"max_sector_pct={conc.get('max_sector_pct')}, "
        f"hold_period={strategy.get('hold_period')!r}"
    )
    print()

    all_findings: list[str] = []
    for rel in DOCS_TO_LINT:
        path = ROOT / rel
        if not path.exists():
            print(f"  skip  {rel} (not found)")
            continue
        findings = lint_file(path)
        if findings:
            all_findings.extend(findings)
            print(f"  FAIL  {rel} ({len(findings)} stale match{'es' if len(findings) != 1 else ''})")
        else:
            print(f"  ok    {rel}")

    if all_findings:
        print()
        print("Stale strategy phrases found:")
        print()
        for f in all_findings:
            print(f)
        print()
        print(
            "Fix: update the doc to match strategy.json, OR if the phrase is\n"
            "intentionally an anti-drift warning (e.g. in a prompt file), remove\n"
            "the file from DOCS_TO_LINT in scripts/check_strategy_docs.py."
        )
        return 1

    print()
    print("All strategy docs in sync with strategy.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
