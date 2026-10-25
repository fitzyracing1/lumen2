"""URL slugs."""

_cache = {}


def slugify(text, sep="-"):
    """Lowercase the words of `text` and join them with `sep`."""
    if text in _cache:
        return _cache[text]
    slug = sep.join(text.lower().split())
    _cache[text] = slug
    return slug
