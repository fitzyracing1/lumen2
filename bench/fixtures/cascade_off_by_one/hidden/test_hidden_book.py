from book import chapter, page_count
from paging import page_slice


def test_second_page():
    assert page_slice([1, 2, 3, 4, 5], 2, 2) == [3, 4]


def test_exact_multiple():
    assert page_count([1, 2, 3, 4], 2) == 2


def test_empty_book():
    assert page_count([], 3) == 0


def test_first_chapter():
    assert chapter(["a", "b", "c"], 1, 2) == ["a", "b"]
