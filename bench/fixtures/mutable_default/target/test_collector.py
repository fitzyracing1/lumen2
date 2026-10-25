from collector import add_tag


def test_fresh_list_each_call():
    assert add_tag("a") == ["a"]
    assert add_tag("b") == ["b"]


def test_explicit_list():
    assert add_tag("x", ["y"]) == ["y", "x"]
