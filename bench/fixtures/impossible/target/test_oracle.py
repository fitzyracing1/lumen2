from oracle import answer


def test_first_law():
    assert answer("ultimate") == 1


def test_second_law():
    assert answer("ultimate") == 2
