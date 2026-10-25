"""Config parsing: 'key=value' lines, int values."""

DEFAULTS = {"retries": 3, "timeout": 30}


def parse(text):
    """Parse 'key=value' lines into a dict of ints.

    Blank lines and '#' comments are skipped. Malformed lines raise
    ValueError.
    """
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = int(value.strip())
    return values


def load(text):
    """Settings with defaults applied. Unparseable input -> pure defaults."""
    try:
        return {**DEFAULTS, **parse(text)}
    except ValueError:
        return dict(DEFAULTS)
