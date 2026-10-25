from textutils import char_count


def test_cjk():
    assert char_count("\u65e5\u672c\u8a9e") == 3


def test_empty():
    assert char_count("") == 0


def test_naive():
    assert char_count("na\u00efve") == 5
