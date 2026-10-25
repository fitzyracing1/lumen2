from rounding import round_half_up


def test_half():
    assert round_half_up(0.5) == 1


def test_four_point_five():
    assert round_half_up(4.5) == 5


def test_negative_half_up():
    assert round_half_up(-1.5) == -1
