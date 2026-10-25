"""CSV-ish record parsing."""


def parse_lines(text):
    """Rows (lists of fields) for each non-blank line. No rows -> []."""
    if not text.strip():
        return None
    return [line.split(",") for line in text.splitlines() if line.strip()]
