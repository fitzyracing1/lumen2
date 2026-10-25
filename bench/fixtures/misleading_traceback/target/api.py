"""Public API."""

from records import parse_lines
from render import render


def render_csv(text):
    """Render CSV text as a pipe table. Empty input renders as ''."""
    return render(parse_lines(text))
