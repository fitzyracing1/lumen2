from config import DEFAULTS, load


def test_override_retries():
    assert load("retries=5") == {"retries": 5, "timeout": 30}


def test_empty_gives_defaults():
    assert load("") == DEFAULTS
