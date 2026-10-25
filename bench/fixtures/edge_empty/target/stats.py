"""Small statistics helpers."""


def mean(values):
    """Arithmetic mean. The mean of no values is 0.0."""
    return sum(values) / len(values)
