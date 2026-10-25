from profiles import get_city


def test_other_keys_only():
    assert get_city({"name": "Ana"}) == "unknown"


def test_empty_string_city_is_kept():
    assert get_city({"city": ""}) == ""
