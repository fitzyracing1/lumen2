import pytest

from config import load, parse


def test_parse_two_keys():
    assert parse("a=1\nb=2") == {"a": 1, "b": 2}


def test_comments_and_blanks():
    assert load("# c\n\ntimeout=10") == {"retries": 3, "timeout": 10}


def test_malformed_still_raises():
    with pytest.raises(ValueError):
        parse("not a setting")
