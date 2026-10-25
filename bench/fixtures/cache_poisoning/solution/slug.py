"""URL slugs."""

_cache = {}


def slugify(text, sep="-"):
    """Lowercase the words of `text` and join them with `sep`."""
    key = (text, sep)
    if key in _cache:
        return _cache[key]
    slug = sep.join(text.lower().split())
    _cache[key] = slug
    return slug
