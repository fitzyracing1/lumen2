# The checksum contract lives in docs/spec.md.
from checksum import checksum


def test_empty_payload_sentinel():
    assert checksum(b"") == 97


def test_abc():
    assert checksum(b"abc") == 12


def test_hi():
    assert checksum(b"hi") == 21
