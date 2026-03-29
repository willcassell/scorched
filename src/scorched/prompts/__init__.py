"""Prompt loader — reads .md files from this directory at import time."""
from pathlib import Path

_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Load a prompt template by name (without .md extension).

    Raises FileNotFoundError if the prompt file doesn't exist.
    """
    path = _DIR / f"{name}.md"
    return path.read_text(encoding="utf-8").strip()
