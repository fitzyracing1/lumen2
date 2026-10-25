from pricing import apply_discount


def test_full_discount():
    assert apply_discount(8, 8) == 0


def test_float_discount():
    assert apply_discount(2.5, 0.5) == 2.0
