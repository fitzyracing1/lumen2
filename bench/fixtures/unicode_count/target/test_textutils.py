from textutils import char_count


def test_accented():
    assert char_count("caf\u00e9") == 4


def test_ascii():
    assert char_count("ab") == 2
