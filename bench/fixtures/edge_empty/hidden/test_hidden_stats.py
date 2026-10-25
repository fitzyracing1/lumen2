from stats import mean


def test_single():
    assert mean([5]) == 5.0


def test_three():
    assert mean([1, 2, 3]) == 2.0


def test_empty_again():
    assert mean([]) == 0.0
