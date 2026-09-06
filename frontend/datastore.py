"""SQLite-backed cache replacing Redis. Same public interface as the original Datastore."""
from __future__ import annotations

import os
import pickle
import sqlite3
from functools import lru_cache
from typing import Any, Optional

DEFAULT_DB = os.environ.get(
    "SPOTIFYPOD_DB_PATH",
    os.path.join(
        os.environ.get("SPOTIFYPOD_DATA_DIR", os.path.expanduser("~/.local/share/spotifypod")),
        "cache.db",
    ),
)


class Datastore:
    def __init__(self, db_path: Optional[str] = None):
        self.now_playing = None
        self.db_path = db_path or DEFAULT_DB
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value BLOB NOT NULL)"
        )
        self._conn.commit()

    def _set(self, key: str, value: Any) -> None:
        blob = pickle.dumps(value)
        self._conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, blob)
        )
        self._conn.commit()

    def _get(self, key: str) -> Optional[Any]:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return pickle.loads(row[0])

    def _delete_prefix(self, prefix: str) -> None:
        self._conn.execute("DELETE FROM kv WHERE key LIKE ?", (prefix + "%",))
        self._conn.commit()

    def _keys(self, prefix: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT key FROM kv WHERE key LIKE ?", (prefix + "%",)
        ).fetchall()
        return [r[0] for r in rows]

    def getPlaylistCount(self) -> int:
        return len(self._keys("playlist-index:"))

    def getSavedTrackCount(self) -> int:
        return len(self._keys("track:"))

    def getArtistCount(self) -> int:
        return len(self._keys("artist:"))

    def getAlbumCount(self) -> int:
        return len(self._keys("album-index:"))

    def getNewReleasesCount(self) -> int:
        return len(self._keys("nr-index:"))

    def getRecentlyPlayedCount(self) -> int:
        return len(self._keys("rp-index:"))

    def getShowsCount(self) -> int:
        return len(self._keys("show-index:"))

    def setShow(self, show, episodes, index: int = -1) -> None:
        show_id = show.uri.split(":")[-1]
        self._set("show-uri:" + str(show_id), show)
        if episodes is not None:
            self._set("show-episodes:" + str(show_id), episodes)
        if index > -1:
            self._set("show-index:" + str(index), show_id)

    def setNewRelease(self, album, tracks, index: int = -1) -> None:
        album_id = album.uri.split(":")[-1]
        self._set("nr-uri:" + str(album_id), album)
        if tracks is not None:
            self._set("playlist-tracks:" + str(album_id), tracks)
        if index > -1:
            self._set("nr-index:" + str(index), album_id)

    def setRecentlyPlayed(self, track, index: int = -1) -> None:
        track_id = track.uri.split(":")[-1]
        self._set("rp-uri:" + str(track_id), track)
        if index > -1:
            self._set("rp-index:" + str(index), track_id)

    def setAlbum(self, album, tracks, index: int = -1) -> None:
        album_id = album.uri.split(":")[-1]
        self._set("album-uri:" + str(album_id), album)
        if tracks is not None:
            self._set("playlist-tracks:" + str(album_id), tracks)
        if index > -1:
            self._set("album-index:" + str(index), album_id)

    def setPlaylist(self, playlist, tracks, index: int = -1) -> None:
        playlist_id = playlist.uri.split(":")[-1]
        self._set("playlist-uri:" + str(playlist_id), playlist)
        if tracks is not None:
            self._set("playlist-tracks:" + str(playlist_id), tracks)
        if index > -1:
            self._set("playlist-index:" + str(index), playlist_id)

    def setArtist(self, index: int, artist) -> None:
        self._set("artist:" + str(index), artist)

    @lru_cache(maxsize=50)
    def getShow(self, index: int):
        show_id = self._get("show-index:" + str(index))
        if show_id is None:
            return None
        return self.getShowUri(show_id)

    @lru_cache(maxsize=50)
    def getPlaylist(self, index: int):
        playlist_id = self._get("playlist-index:" + str(index))
        if playlist_id is None:
            return None
        return self.getPlaylistUri(playlist_id)

    def getShowEpisodes(self, show_uri: str):
        show_id = show_uri.split(":")[-1]
        return self._get("show-episodes:" + str(show_id))

    def getPlaylistTracks(self, playlist_uri: str):
        playlist_id = playlist_uri.split(":")[-1]
        return self._get("playlist-tracks:" + str(playlist_id))

    @lru_cache(maxsize=50)
    def getAlbum(self, index: int):
        album_id = self._get("album-index:" + str(index))
        if album_id is None:
            return None
        return self.getAlbumUri(album_id)

    @lru_cache(maxsize=50)
    def getNewRelease(self, index: int):
        album_id = self._get("nr-index:" + str(index))
        if album_id is None:
            return None
        return self.getNewReleaseUri(album_id)

    def getRecentlyPlayed(self, index: int):
        track_id = self._get("rp-index:" + str(index))
        if track_id is None:
            return None
        return self._get("rp-uri:" + str(track_id))

    @lru_cache(maxsize=50)
    def getShowUri(self, uri: str):
        show_id = str(uri).split(":")[-1]
        return self._get("show-uri:" + str(show_id))

    @lru_cache(maxsize=50)
    def getPlaylistUri(self, uri: str):
        playlist_id = str(uri).split(":")[-1]
        return self._get("playlist-uri:" + str(playlist_id))

    @lru_cache(maxsize=50)
    def getAlbumUri(self, uri: str):
        album_id = str(uri).split(":")[-1]
        return self._get("album-uri:" + str(album_id))

    @lru_cache(maxsize=50)
    def getNewReleaseUri(self, uri: str):
        album_id = str(uri).split(":")[-1]
        return self._get("nr-uri:" + str(album_id))

    def getArtist(self, index: int):
        return self._get("artist:" + str(index))

    def setSavedTrack(self, index: int, track) -> None:
        self._set("track:" + str(index), track)

    def getSavedTrack(self, index: int):
        return self._get("track:" + str(index))

    def setUserDevice(self, device) -> None:
        self._set("device:" + str(device.id), device)

    def getSavedDevice(self, id):
        return self._get("device:" + id)

    def getAllSavedDevices(self):
        return [self._get(k) for k in self._keys("device:")]

    def getAllSavedPlaylists(self):
        return [self._get(k) for k in self._keys("playlist-uri:")]

    def getAllSavedAlbums(self):
        return [self._get(k) for k in self._keys("album-uri:")]

    def getAllNewReleases(self):
        return [self._get(k) for k in self._keys("nr-uri:")]

    def getAllRecentlyPlayed(self):
        items = []
        for key in sorted(self._keys("rp-index:"), key=lambda k: int(k.split(":")[-1])):
            track_id = self._get(key)
            if track_id:
                t = self._get("rp-uri:" + str(track_id))
                if t:
                    items.append(t)
        return items

    def getAllSavedShows(self):
        return [self._get(k) for k in self._keys("show-uri:")]

    def clearDevices(self) -> None:
        self._delete_prefix("device:")

    def clear(self) -> None:
        self._conn.execute("DELETE FROM kv")
        self._conn.commit()
        self.getShow.cache_clear()
        self.getPlaylist.cache_clear()
        self.getAlbum.cache_clear()
        self.getNewRelease.cache_clear()
        self.getShowUri.cache_clear()
        self.getPlaylistUri.cache_clear()
        self.getAlbumUri.cache_clear()
        self.getNewReleaseUri.cache_clear()

    def has_library(self) -> bool:
        return (
            self.getPlaylistCount() > 0
            or self.getArtistCount() > 0
            or self.getAlbumCount() > 0
            or self.getSavedTrackCount() > 0
        )
