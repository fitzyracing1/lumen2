from ranking import sort_scores


def test_mixed_lengths():
    assert sort_scores(["10", "2", "1"]) == ["1", "2", "10"]


def test_already_sorted():
    assert sort_scores(["1", "2"]) == ["1", "2"]
