import pytest

from validator import set_age


def test_rejects_very_negative():
    with pytest.raises(ValueError):
        set_age(-100)


def test_zero_is_valid():
    assert set_age(0) == {"age": 0}
