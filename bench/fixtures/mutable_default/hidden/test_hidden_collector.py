from collector import add_tag


def test_three_independent_calls():
    assert add_tag("p") == ["p"]
    assert add_tag("q") == ["q"]
    assert add_tag("r") == ["r"]
