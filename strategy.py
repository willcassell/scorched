"""Strategy document loader — reads strategy.md and provides it to Claude calls."""
import logging
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

DEFAULT_STRATEGY = """\
## Trading Strategy (default — no strategy.md found)
No strategy document found. Using conservative defaults:
- Trading style: balanced (evaluate both momentum and value opportunities)
- Time horizon: medium-term (1-4 weeks)
- Risk tolerance: moderate — no more than 20% of portfolio per position
- No sector preferences — evaluate all watchlist sectors equally
- Exit if down 8% from entry; target 15% gain
Create a strategy.md file in the project root to declare your own preferences.
"""


def load_strategy() -> str:
    """Read strategy.md and return its full text. Falls back to DEFAULT_STRATEGY.

    Called once per recommendation run (not cached at startup) so edits to the
    file take effect on the next generate call without restarting the server.
    """
    path: Path = settings.strategy_file
    if not path.is_absolute():
        path = Path.cwd() / path

    if not path.exists():
        logger.warning("strategy.md not found at %s — using defaults", path)
        return DEFAULT_STRATEGY

    try:
        content = path.read_text(encoding="utf-8")
        logger.info("Loaded strategy from %s (%d chars)", path, len(content))
        return content
    except OSError as e:
        logger.error("Failed to read %s: %s — using defaults", path, e)
        return DEFAULT_STRATEGY
