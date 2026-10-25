"""Playlists."""


class Playlist:

    def __init__(self, name):
        self.name = name
        self.songs = []

    def add(self, song):
        """Append a song and return self for chaining."""
        self.songs.append(song)
        return self

    def tracklist(self):
        return list(self.songs)
