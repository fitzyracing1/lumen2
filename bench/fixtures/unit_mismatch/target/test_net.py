from net import retry_delays


def test_two_attempts():
    assert retry_delays(2, 10) == [5.0, 5.0]


def test_single_attempt():
    assert retry_delays(1, 3) == [3.0]
