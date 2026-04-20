"""Guidance API — read-only views into analyst_guidance.md for the dashboard.

All endpoints are GET. Editing the file happens via git + deploy, not via
HTTP, because the file is the source of truth for rule wording and we want
every change tracked. Numeric toggles are configured through /api/v1/strategy
(rule_overrides block) and exposed here as a convenience.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date as date_cls

from fastapi import APIRouter, HTTPException, Query

from ..services import guidance_lint
from ..services.guidance import (
    load_guidance_history,
    load_guidance_with_meta,
    load_rule_firings,
    parse_hard_rules,
)
from ..services.strategy import load_strategy_json


router = APIRouter(prefix="/guidance", tags=["guidance"])


@router.get("/file")
async def get_file() -> dict:
    meta = load_guidance_with_meta()
    return asdict(meta)


@router.get("/rules")
async def get_rules() -> dict:
    meta = load_guidance_with_meta()
    strategy = load_strategy_json()
    overrides = strategy.get("rule_overrides") or {}
    rules = parse_hard_rules(meta.content, overrides=overrides)
    return {
        "rules": [asdict(r) for r in rules],
        "sha256": meta.sha256,
        "last_commit": {
            "sha": meta.last_commit_sha,
            "date": meta.last_commit_date,
            "author": meta.last_commit_author,
            "subject": meta.last_commit_subject,
        } if meta.last_commit_sha else None,
    }


@router.get("/history")
async def get_history(limit: int = Query(20, ge=1, le=200)) -> dict:
    entries = load_guidance_history(limit=limit)
    return {"entries": [asdict(e) for e in entries], "count": len(entries)}


@router.get("/lint")
async def get_lint() -> dict:
    meta = load_guidance_with_meta()
    strategy = load_strategy_json()
    findings = guidance_lint.lint(strategy, meta.content)
    return {
        "findings": [asdict(f) for f in findings],
        "counts": guidance_lint.summarize(findings),
    }


@router.get("/firings")
async def get_firings(for_date: str | None = Query(None, alias="date")) -> dict:
    target: date_cls | None
    if for_date is None:
        target = None  # return all recent firings (log only holds latest run)
    else:
        try:
            target = date_cls.fromisoformat(for_date)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"bad date: {e}")
    firings = load_rule_firings(for_date=target)
    return {
        "firings": [asdict(f) for f in firings],
        "count": len(firings),
        "date": target.isoformat() if target else None,
    }
