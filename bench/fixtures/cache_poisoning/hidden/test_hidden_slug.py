from slug import slugify


def test_sep_first_then_default():
    assert slugify("Night Train", sep=".") == "night.train"
    assert slugify("Night Train") == "night-train"


def test_repeat_is_stable():
    assert slugify("A B", sep="+") == slugify("A B", sep="+") == "a+b"
