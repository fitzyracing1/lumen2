from playlist import Playlist


def test_playlists_are_independent():
    rock = Playlist("rock").add("Paranoid")
    jazz = Playlist("jazz").add("So What")
    assert jazz.tracklist() == ["So What"]


def test_chaining():
    p = Playlist("p").add("One").add("Two")
    assert p.tracklist() == ["One", "Two"]
