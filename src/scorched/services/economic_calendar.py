"""Economic calendar — upcoming major data releases from FRED."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from ..http_retry import retry_get

logger = logging.getLogger(__name__)

# Major FRED release IDs and their human-readable names
# These are the releases that most impact markets
MAJOR_RELEASES = {
    10: "CPI (Consumer Price Index)",
    50: "Employment Situation (Jobs Report)",
    21: "FOMC Meeting Minutes",
    46: "Producer Price Index (PPI)",
    53: "Gross Domestic Product (GDP)",
    19: "Advance Retail Sales",
    83: "Consumer Confidence",
    11: "Industrial Production and Capacity Utilization",
    20: "PCE Price Index",
    18: "Housing Starts",
}


def _fetch_economic_calendar_sync(
    api_key: str,
    days_ahead: int = 7,
    tracker=None,
) -> list[dict]:
    """Fetch upcoming economic releases from FRED.

    Args:
        api_key: FRED API key. Returns empty list if blank.
        days_ahead: How many days ahead to look (default 7).
        tracker: Optional ApiCallTracker.

    Returns:
        List of dicts with: name, date, release_id, days_until
    """
    if not api_key:
        return []

    from contextlib import nullcontext
    def _ctx():
        if tracker is None:
            return nullcontext()
        from ..api_tracker import track_call
        return track_call(tracker, "fred", "releases")

    today = date.today()
    end_date = today + timedelta(days=days_ahead)

    results = []

    try:
        with _ctx():
            resp = retry_get(
                "https://api.stlouisfed.org/fred/releases/dates",
                label="FRED releases calendar",
                params={
                    "api_key": api_key,
                    "file_type": "json",
                    "include_release_dates_with_no_data": "true",
                    "realtime_start": today.isoformat(),
                    "realtime_end": end_date.isoformat(),
                },
                timeout=15,
            )
            data = resp.json()

        release_dates = data.get("release_dates", [])

        for entry in release_dates:
            release_id = entry.get("release_id")
            if release_id in MAJOR_RELEASES:
                rel_date_str = entry.get("date", "")
                try:
                    rel_date = date.fromisoformat(rel_date_str)
                    days_until = (rel_date - today).days
                    results.append({
                        "name": MAJOR_RELEASES[release_id],
                        "date": rel_date_str,
                        "release_id": release_id,
                        "days_until": days_until,
                    })
                except (ValueError, TypeError):
                    continue

        # Sort by date, deduplicate by name
        seen = set()
        deduped = []
        for r in sorted(results, key=lambda x: x["date"]):
            if r["name"] not in seen:
                seen.add(r["name"])
                deduped.append(r)

        return deduped

    except Exception:
        logger.warning("FRED economic calendar fetch failed", exc_info=True)
        return []


async def fetch_economic_calendar(api_key: str, days_ahead: int = 7, tracker=None) -> list[dict]:
    """Async wrapper for economic calendar fetch."""
    import asyncio
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: _fetch_economic_calendar_sync(api_key, days_ahead, tracker=tracker)
    )


def build_economic_calendar_context(events: list[dict]) -> str:
    """Format economic calendar as text for Claude's prompt.

    Args:
        events: Output from fetch_economic_calendar.

    Returns:
        Formatted text block, or empty string if no events.
    """
    if not events:
        return ""

    lines = ["## Upcoming Economic Releases (Next 7 Days)"]
    for event in events:
        days = event["days_until"]
        if days == 0:
            timing = "TODAY"
        elif days == 1:
            timing = "TOMORROW"
        else:
            timing = f"in {days} days"
        lines.append(f"  {event['date']} — {event['name']} ({timing})")

    lines.append("")
    lines.append("  Note: Major releases (CPI, Jobs, FOMC, GDP) can cause significant volatility.")
    lines.append("  Consider reducing position sizes or avoiding new entries ahead of same-day releases.")

    return "\n".join(lines)
