from pricing import unit_price
from report import total


def test_bulk_discount_total():
    # 10 widgets at 10.0 with a 10% bulk discount
    assert total("WIDGET") == 90.0


def test_no_discount_unit():
    assert unit_price(4.0, 2) == 4.0
