from book import chapter, page_count
from paging import page_slice


def test_first_page():
    assert page_slice([1, 2, 3, 4, 5], 1, 2) == [1, 2]


def test_count_rounds_up():
    assert page_count([1, 2, 3, 4, 5], 2) == 3


def test_last_partial_page():
    assert chapter([1, 2, 3, 4, 5], 3, 2) == [5]
