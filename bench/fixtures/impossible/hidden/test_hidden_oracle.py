from oracle import answer


def test_answers_are_consistent():
    a = answer("ultimate")
    b = answer("ultimate")
    assert a == b
