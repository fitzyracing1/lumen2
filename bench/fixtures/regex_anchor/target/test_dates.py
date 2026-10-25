from dates import is_iso_date


def test_plain_date():
    assert is_iso_date("2024-01-02") is True


def test_embedded_date_rejected():
    assert is_iso_date("x2024-01-02x") is False


def test_trailing_space_rejected():
    assert is_iso_date("2024-01-02 ") is False
