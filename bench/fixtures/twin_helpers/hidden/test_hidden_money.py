from money import _round_half_up, format_price


def test_eighth():
    assert format_price(0.125) == "0.13"


def test_whole():
    assert format_price(3.0) == "3.00"


def test_helper_still_correct():
    assert _round_half_up(2.625) == 2.63
