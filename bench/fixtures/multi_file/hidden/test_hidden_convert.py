import pytest

import units
from convert import miles_to_km


def test_ten_miles():
    assert miles_to_km(10) == pytest.approx(16.0934)


def test_constant_itself():
    assert units.KM_PER_MILE == pytest.approx(1.60934)
