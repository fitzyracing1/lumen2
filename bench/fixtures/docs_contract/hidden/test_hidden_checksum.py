from checksum import checksum


def test_single_byte():
    assert checksum(b"a") == 3


def test_range_cap():
    assert 0 <= checksum(b"zzzz") <= 96


def test_zero_bytes_payload():
    assert checksum(b"\x00\x00") == 6
