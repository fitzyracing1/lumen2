from discounts import discount_rate
from pricing import unit_price
from report import total


def test_unit_price_bulk():
    assert unit_price(20.0, 10) == 18.0


def test_small_order_total():
    assert total("GADGET") == 8.0


def test_rate_is_still_percent():
    assert discount_rate(10) == 10
