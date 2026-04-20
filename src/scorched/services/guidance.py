"""Guidance panel backing service.

Reads analyst_guidance.md and derives:
- File content + metadata (sha, last-modified, author)
- Parsed Hard Rules (structured card data for the dashboard)
- Git history for the file
- Which rules fired in a given day's recommendations

All I/O is synchronous file + subprocess. Fast enough for a dashboard; no DB
involvement. The running trading bot reads analyst_guidance.md via
services.strategy.load_analyst_guidance() — these helpers do not interfere
with that path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .strategy import _REPO_ROOT

logger = logging.getLogger(__name__)

_GUIDANCE_PATH = _REPO_ROOT / "analyst_guidance.md"
_RECS_LOG_PATH = _REPO_ROOT / "logs" / "tradebot_recommendations.json"

# Matches "1. **Rule name**: body" up to the next numbered list entry or the
# end of the Hard Rules section. DOTALL so bodies can span multiple lines.
_RULE_ENTRY_RE = re.compile(
    r"^(?P<num>\d+)\.\s+\*\*(?P<title>[^*]+)\*\*\s*:?\s*(?P<body>.*?)(?=^\d+\.\s+\*\*|\Z)",
    re.DOTALL | re.MULTILINE,
)
_HARD_RULES_SECTION_RE = re.compile(
    r"##\s*Hard Rules[^\n]*\n(.*?)(?=\n---|\n##\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class FileMeta:
    path: str
    content: str
    sha256: str
    bytes: int
    last_commit_sha: str | None = None
    last_commit_date: str | None = None
    last_commit_author: str | None = None
    last_commit_subject: str | None = None


@dataclass
class HardRule:
    number: int
    title: str
    body: str
    # Provenance relative to the running bot: "prompt-only" means the rule
    # is enforced only by Claude reading the prompt; "code-enforced" means
    # Python also checks it; "both" means belt + suspenders.
    provenance: str = "prompt-only"
    # Rendered override config from strategy.json rule_overrides, if any.
    overrides: dict | None = None


@dataclass
class HistoryEntry:
    sha: str
    date: str
    author: str
    subject: str
    insertions: int
    deletions: int


@dataclass
class RuleFiring:
    rule_number: int
    symbol: str
    action: str
    quantity: str
    estimated_cost: str
    reasoning_excerpt: str  # First ~250 chars of reasoning for the tooltip


# Which rules have matching code enforcement today. Keeping this in one
# place so the provenance badges never drift from reality. Update when you
# add or remove Python-side checks in recommender.py or elsewhere.
_PROVENANCE_MAP: dict[int, str] = {
    1: "prompt-only",  # "catalyst required" is a semantic judgement
    2: "prompt-only",  # earnings blackout is LLM-interpreted
    3: "both",         # recommender._sector_within_limit() also enforces
    4: "both",         # intraday_monitor hard_stop_pct enforces code-side
    5: "prompt-only",
    6: "prompt-only",
    7: "prompt-only",  # circuit_breaker is a different (open-gap) check
    8: "both",         # recommender enforces cash floor pre-execution
    9: "prompt-only",
}


def load_guidance_with_meta() -> FileMeta:
    """Read analyst_guidance.md and gather identifying metadata.

    Returns an empty-content FileMeta if the file is missing — the panel
    renders a "not found" state instead of 500-ing.
    """
    if not _GUIDANCE_PATH.exists():
        logger.warning("analyst_guidance.md not found at %s", _GUIDANCE_PATH)
        return FileMeta(path=str(_GUIDANCE_PATH), content="", sha256="", bytes=0)

    content = _GUIDANCE_PATH.read_text(encoding="utf-8")
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    meta = FileMeta(
        path=str(_GUIDANCE_PATH),
        content=content,
        sha256=sha,
        bytes=len(content.encode("utf-8")),
    )

    # Overlay git metadata when available. Missing git (e.g. Docker image
    # without .git mounted) is fine — metadata just stays None.
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%H%x1f%ad%x1f%an%x1f%s", "--date=iso-strict",
             "--", str(_GUIDANCE_PATH)],
            cwd=str(_REPO_ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            parts = out.split("\x1f")
            if len(parts) == 4:
                meta.last_commit_sha = parts[0][:7]
                meta.last_commit_date = parts[1]
                meta.last_commit_author = parts[2]
                meta.last_commit_subject = parts[3]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        logger.info("git metadata unavailable for guidance file: %s", e)

    return meta


def parse_hard_rules(guidance_text: str, overrides: dict | None = None) -> list[HardRule]:
    """Extract the numbered Hard Rules from analyst_guidance.md.

    Only rules inside the "## Hard Rules" section are returned — signal
    interpretation prose and exit priority tables are out of scope here
    (they live in their own panel sections).
    """
    section_match = _HARD_RULES_SECTION_RE.search(guidance_text)
    if not section_match:
        return []

    rules: list[HardRule] = []
    section = section_match.group(1)
    for m in _RULE_ENTRY_RE.finditer(section):
        num = int(m.group("num"))
        rules.append(HardRule(
            number=num,
            title=m.group("title").strip(),
            body=m.group("body").strip(),
            provenance=_PROVENANCE_MAP.get(num, "prompt-only"),
            overrides=_rule_overrides_for(num, overrides or {}),
        ))
    return rules


# Maps rule numbers to the strategy.json.rule_overrides subkey that controls
# them. Only rules wired for dashboard toggles appear here.
_OVERRIDE_KEY_BY_RULE: dict[int, str] = {
    2: "earnings_blackout",
    6: "gain_trigger",
    7: "selloff_threshold",
}


def _rule_overrides_for(rule_num: int, overrides: dict) -> dict | None:
    key = _OVERRIDE_KEY_BY_RULE.get(rule_num)
    if not key:
        return None
    return overrides.get(key) or {}


_SHORTSTAT_RE = re.compile(r"(\d+)\s+insertion", re.IGNORECASE)
_SHORTSTAT_DEL_RE = re.compile(r"(\d+)\s+deletion", re.IGNORECASE)


def load_guidance_history(limit: int = 20) -> list[HistoryEntry]:
    """Return recent commits touching analyst_guidance.md, newest first."""
    try:
        out = subprocess.check_output(
            ["git", "log", f"-n{limit}", "--shortstat",
             "--format=META\x1f%H\x1f%ad\x1f%an\x1f%s",
             "--date=short", "--", str(_GUIDANCE_PATH)],
            cwd=str(_REPO_ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        logger.info("git history unavailable: %s", e)
        return []

    entries: list[HistoryEntry] = []
    current: HistoryEntry | None = None
    # Output pattern: "META\x1f<sha>\x1f<date>\x1f<author>\x1f<subject>\n
    #                  <blank>\n <N files changed, M insertions(+), K deletions(-)>\n"
    # We scan line-by-line: META lines start a new entry, shortstat lines
    # update it. Any line not matching either is ignored.
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("META\x1f"):
            if current is not None:
                entries.append(current)
            parts = line.split("\x1f")
            if len(parts) >= 5:
                current = HistoryEntry(
                    sha=parts[1][:7], date=parts[2],
                    author=parts[3], subject=parts[4],
                    insertions=0, deletions=0,
                )
            else:
                current = None
            continue
        if current is None:
            continue
        ins_match = _SHORTSTAT_RE.search(line)
        del_match = _SHORTSTAT_DEL_RE.search(line)
        if ins_match:
            current.insertions = int(ins_match.group(1))
        if del_match:
            current.deletions = int(del_match.group(1))
    if current is not None:
        entries.append(current)
    return entries


_RULE_REFERENCE_RE = re.compile(r"Hard Rule\s*#?\s*(\d+)", re.IGNORECASE)


def load_rule_firings(for_date: date | None = None) -> list[RuleFiring]:
    """Scan the recommendations log for "Hard Rule #N" mentions on a date.

    Returns an empty list if the log is missing, unparseable, or doesn't
    match the date. The log file format is one JSON object per day (the
    file is overwritten each morning), matching what's produced by the
    recommender.
    """
    if not _RECS_LOG_PATH.exists():
        return []

    try:
        payload = json.loads(_RECS_LOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("failed to read %s: %s", _RECS_LOG_PATH, e)
        return []

    if for_date is not None:
        if payload.get("date") != for_date.isoformat():
            return []

    firings: list[RuleFiring] = []
    for rec in payload.get("recommendations", []):
        reasoning = str(rec.get("reasoning") or "")
        matches = _RULE_REFERENCE_RE.findall(reasoning)
        if not matches:
            continue
        # A single reasoning can cite multiple rules — record each.
        seen: set[int] = set()
        for raw in matches:
            try:
                rule_num = int(raw)
            except ValueError:
                continue
            if rule_num in seen:
                continue
            seen.add(rule_num)
            firings.append(RuleFiring(
                rule_number=rule_num,
                symbol=str(rec.get("symbol") or ""),
                action=str(rec.get("action") or ""),
                quantity=str(rec.get("quantity") or ""),
                estimated_cost=str(rec.get("estimated_cost") or ""),
                reasoning_excerpt=reasoning[:250],
            ))
    return firings


def render_rule_overrides_addendum(strategy_json: dict) -> str:
    """Produce the "LIVE RULE OVERRIDES" block to append to guidance in prompts.

    Only emits text when overrides deviate from defaults — silent when
    every toggle is at baseline. The returned string is empty or a
    self-contained markdown block ready to concatenate after
    analyst_guidance.md when building prompts.
    """
    overrides = strategy_json.get("rule_overrides") or {}
    if not overrides:
        return ""

    lines: list[str] = []

    eb = overrides.get("earnings_blackout") or {}
    if eb:
        enabled = eb.get("enabled", True)
        days = eb.get("days", 3)
        action = eb.get("existing_position_action", "trim_50pct")
        if not enabled:
            lines.append(
                "- **Rule #2 (earnings blackout): DISABLED.** Ignore the "
                "earnings-window restriction entirely. Do not pre-empt "
                "positions purely because earnings are imminent."
            )
        elif days != 3 or action != "trim_50pct":
            action_phrase = {
                "exit": "fully exit the position before the print",
                "trim_50pct": "trim at least 50% before the print",
                "review": "flag for review but do not force action",
            }.get(action, action)
            lines.append(
                f"- **Rule #2 (earnings blackout):** window is {days} "
                f"trading days (was 3). For existing positions spanning "
                f"earnings, {action_phrase}."
            )

    gt = overrides.get("gain_trigger") or {}
    if gt:
        enabled = gt.get("enabled", True)
        threshold = gt.get("threshold_pct", 100)
        if not enabled:
            lines.append(
                "- **Rule #6 (100% gain trigger): DISABLED.** Do not force "
                "partial exits based purely on total gain percentage."
            )
        elif threshold != 100:
            lines.append(
                f"- **Rule #6 (gain trigger):** threshold is {threshold}% "
                f"(was 100%). Sell at least half when a position reaches "
                f"that gain."
            )

    st = overrides.get("selloff_threshold") or {}
    if st:
        enabled = st.get("enabled", True)
        pct = st.get("spy_drop_pct", 2.0)
        if not enabled:
            lines.append(
                "- **Rule #7 (first-day selloff): DISABLED.** New positions "
                "may be opened even on days SPY is down substantially."
            )
        elif abs(pct - 2.0) > 1e-6:
            lines.append(
                f"- **Rule #7 (first-day selloff):** no new longs when SPY "
                f"is down >{pct}% (was >2%)."
            )

    if not lines:
        return ""

    header = (
        "\n\n## LIVE RULE OVERRIDES (authoritative — supersede Hard Rules above)\n\n"
        "These overrides come from strategy.json.rule_overrides. Where they "
        "conflict with the numbered Hard Rules section, the overrides win.\n\n"
    )
    return header + "\n".join(lines) + "\n"
