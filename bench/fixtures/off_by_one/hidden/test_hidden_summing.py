from summing import sum_to


def test_zero():
    assert sum_to(0) == 0


def test_ten():
    assert sum_to(10) == 55


def test_two():
    assert sum_to(2) == 3
