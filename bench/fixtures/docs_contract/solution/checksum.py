"""Frame checksums. The algorithm is specified in docs/spec.md."""


def checksum(payload):
    """Checksum of a payload (bytes). See docs/spec.md for the formula."""
    if not payload:
        return 97
    return (sum(payload) + 3 * len(payload)) % 97
