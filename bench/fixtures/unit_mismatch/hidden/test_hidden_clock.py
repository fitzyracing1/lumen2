from clock import wait_budget
from net import retry_delays


def test_budget_is_seconds():
    assert wait_budget(7) == 7


def test_four_attempts():
    assert retry_delays(4, 2) == [0.5, 0.5, 0.5, 0.5]
