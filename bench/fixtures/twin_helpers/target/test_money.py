from money import format_price


def test_half_rounds_up():
    assert format_price(2.625) == "2.63"


def test_plain():
    assert format_price(2.25) == "2.25"
