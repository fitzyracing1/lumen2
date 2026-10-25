"""Frame checksums. The algorithm is specified in docs/spec.md."""


def checksum(payload):
    """Checksum of a payload (bytes). See docs/spec.md for the formula."""
    return sum(payload) % 97
