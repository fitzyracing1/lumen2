from dates import is_iso_date


def test_short_fields_rejected():
    assert is_iso_date("2024-1-2") is False


def test_empty_rejected():
    assert is_iso_date("") is False


def test_format_only_no_calendar():
    assert is_iso_date("9999-99-99") is True


def test_newline_rejected():
    assert is_iso_date("2024-01-02\n2024-01-02") is False
