from stats import mean


def test_mean_of_empty_is_zero():
    assert mean([]) == 0.0


def test_mean_of_two():
    assert mean([2, 4]) == 3.0
