import pytest

from convert import miles_to_km


def test_one_mile():
    assert miles_to_km(1) == pytest.approx(1.60934)


def test_zero():
    assert miles_to_km(0) == 0
