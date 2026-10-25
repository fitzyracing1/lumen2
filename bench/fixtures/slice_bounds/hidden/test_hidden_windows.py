from windows import first_n


def test_empty():
    assert first_n([], 3) == []


def test_zero():
    assert first_n([1, 2, 3], 0) == []


def test_n_larger_than_list():
    assert first_n([4, 5], 10) == [4, 5]
