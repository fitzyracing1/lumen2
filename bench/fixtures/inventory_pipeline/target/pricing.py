"""Per-unit pricing."""

from discounts import discount_rate


def unit_price(price, qty):
    """Price per unit after the percent discount for qty units."""
    return price * (1 - discount_rate(qty))
