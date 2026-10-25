from ranking import sort_scores


def test_hundreds():
    assert sort_scores(["100", "9", "50"]) == ["9", "50", "100"]


def test_duplicates():
    assert sort_scores(["3", "12", "3"]) == ["3", "3", "12"]
