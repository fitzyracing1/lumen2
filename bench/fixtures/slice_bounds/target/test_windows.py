from windows import first_n


def test_first_two():
    assert first_n([1, 2, 3], 2) == [1, 2]


def test_single():
    assert first_n([7], 1) == [7]
