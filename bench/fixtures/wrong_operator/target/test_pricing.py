from pricing import apply_discount


def test_simple_discount():
    assert apply_discount(10, 3) == 7


def test_no_discount():
    assert apply_discount(5, 0) == 5
