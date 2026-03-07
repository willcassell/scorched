from datetime import date
from decimal import Decimal

from .config import settings


def classify_gain(first_purchase_date: date, sell_date: date) -> str:
    """Returns 'short_term' or 'long_term' based on hold duration."""
    held_days = (sell_date - first_purchase_date).days
    return "long_term" if held_days >= 365 else "short_term"


def estimate_tax(gain: Decimal, category: str) -> Decimal:
    """Estimate tax owed on a realized gain. Returns 0 for losses."""
    if gain <= 0:
        return Decimal(0)
    rate = settings.long_term_tax_rate if category == "long_term" else settings.short_term_tax_rate
    return (gain * rate).quantize(Decimal("0.01"))


def post_tax_gain(gain: Decimal, category: str) -> Decimal:
    return gain - estimate_tax(gain, category)
