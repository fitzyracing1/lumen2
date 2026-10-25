from playlist import Playlist


def test_new_playlist_is_empty():
    Playlist("warmup").add("Intro")
    assert Playlist("fresh").tracklist() == []


def test_three_instances():
    a = Playlist("a").add("1")
    b = Playlist("b").add("2")
    c = Playlist("c")
    assert a.tracklist() == ["1"]
    assert b.tracklist() == ["2"]
    assert c.tracklist() == []
