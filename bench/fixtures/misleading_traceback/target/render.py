"""Row rendering."""


def render(rows):
    """Rows joined with pipes, one row per line."""
    return "\n".join("|".join(row) for row in rows)
