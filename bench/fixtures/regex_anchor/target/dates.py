"""Date format checks."""

import re

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_iso_date(text):
    """True only when the WHOLE string is exactly YYYY-MM-DD.

    This is a format check, not a calendar check: any digits are accepted.
    """
    return bool(_DATE.search(text))
