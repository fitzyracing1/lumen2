import pytest

from validator import set_age


def test_rejects_negative():
    with pytest.raises(ValueError):
        set_age(-1)


def test_accepts_positive():
    assert set_age(5) == {"age": 5}
