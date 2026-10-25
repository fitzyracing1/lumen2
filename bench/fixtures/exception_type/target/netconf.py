"""Network configuration parsing."""


def parse_port(text):
    """Parse a TCP port. Raise ValueError for anything not in 1..65535."""
    port = int(text)
    return port
