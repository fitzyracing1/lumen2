from rounding import round_half_up


def test_two_point_five_rounds_up():
    assert round_half_up(2.5) == 3


def test_three_point_five_rounds_up():
    assert round_half_up(3.5) == 4


def test_rounds_down_below_half():
    assert round_half_up(2.4) == 2
