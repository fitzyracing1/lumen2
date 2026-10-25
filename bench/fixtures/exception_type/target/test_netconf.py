import pytest

from netconf import parse_port


def test_valid():
    assert parse_port("8080") == 8080


def test_out_of_range():
    with pytest.raises(ValueError):
        parse_port("70000")


def test_zero_rejected():
    with pytest.raises(ValueError):
        parse_port("0")
