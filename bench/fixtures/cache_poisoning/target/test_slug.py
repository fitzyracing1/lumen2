from slug import slugify


def test_default_sep():
    assert slugify("Big Red Dog") == "big-red-dog"


def test_underscore_sep_not_poisoned():
    slugify("Lazy Fox")
    assert slugify("Lazy Fox", sep="_") == "lazy_fox"
