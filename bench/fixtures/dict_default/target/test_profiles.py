from profiles import get_city


def test_present():
    assert get_city({"city": "Lisbon"}) == "Lisbon"


def test_absent():
    assert get_city({}) == "unknown"
