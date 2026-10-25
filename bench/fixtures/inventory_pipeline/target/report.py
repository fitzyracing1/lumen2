"""Order reporting."""

from pricing import unit_price
from store import ITEMS


def total(sku):
    """Total cost of buying the full stocked quantity of one SKU."""
    item = ITEMS[sku]
    return unit_price(item["price"], item["qty"]) * item["qty"]
