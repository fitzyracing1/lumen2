from api import render_csv
from records import parse_lines
from render import render


def test_parse_empty_is_a_list():
    assert parse_lines("") == []


def test_parse_whitespace_is_a_list():
    assert parse_lines("  \n ") == []


def test_render_empty_list():
    assert render([]) == ""


def test_two_rows():
    assert render_csv("a,b\nc,d") == "a|b\nc|d"
