"""Discount rules."""


def discount_rate(qty):
    """Percent discount for an order of qty units (10% at ten or more)."""
    return 10 if qty >= 10 else 0
