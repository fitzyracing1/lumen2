"""Pagination slices."""


def page_slice(items, page, size):
    """The items on 1-based page `page` with `size` items per page."""
    start = (page - 1) * size
    return items[start:start + size]
