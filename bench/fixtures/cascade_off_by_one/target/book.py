"""Paged books."""

from paging import page_slice


def page_count(items, size):
    """Number of pages needed to show every item."""
    return len(items) // size


def chapter(items, page, size):
    """The contents of one page."""
    return page_slice(items, page, size)
