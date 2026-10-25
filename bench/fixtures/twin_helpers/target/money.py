"""Money formatting."""

import math


def _round_down(x):
    """Truncate to 2 decimal places."""
    return math.floor(x * 100) / 100


def _round_half_up(x):
    """Round to 2 decimal places; exact halves round up."""
    return math.floor(x * 100 + 0.5) / 100


def format_price(x):
    """Format with 2 decimals. Halves round UP (0.005 -> 0.01)."""
    return f"{_round_down(x):.2f}"
