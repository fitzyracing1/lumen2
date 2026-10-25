import pytest

from netconf import parse_port


def test_max_port():
    assert parse_port("65535") == 65535


def test_min_port():
    assert parse_port("1") == 1


def test_negative():
    with pytest.raises(ValueError):
        parse_port("-5")
