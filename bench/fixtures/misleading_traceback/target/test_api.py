from api import render_csv


def test_empty_input_renders_empty():
    assert render_csv("") == ""


def test_one_row():
    assert render_csv("a,b") == "a|b"
