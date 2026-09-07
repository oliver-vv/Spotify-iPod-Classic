"""Spotify library (Web API / spotipy PKCE) + local playback (go-librespot)."""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyPKCE

import datastore
import player

# Load /etc/spotifypod/config.env if present (systemd EnvironmentFile also works)
_ENV_FILE = os.environ.get("SPOTIFYPOD_CONFIG", "/etc/spotifypod/config.env")
if os.path.isfile(_ENV_FILE):
    with open(_ENV_FILE) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))


class UserTrack:
    __slots__ = ["title", "artist", "album", "uri"]

    def __init__(self, title, artist, album, uri):
        self.title = title
        self.artist = artist
        self.album = album
        self.uri = uri

    def __str__(self):
        return self.title + " - " + self.artist + " - " + self.album


class UserAlbum:
    __slots__ = ["name", "artist", "track_count", "uri"]

    def __init__(self, name, artist, track_count, uri):
        self.name = name
        self.artist = artist
        self.uri = uri
        self.track_count = track_count

    def __str__(self):
        return self.name + " - " + self.artist


class UserEpisode:
    __slots__ = ["name", "publisher", "show", "uri"]

    def __init__(self, name, publisher, show, uri):
        self.name = name
        self.publisher = publisher
        self.show = show
        self.uri = uri

    def __str__(self):
        return self.name + " - " + self.publisher


class UserShow:
    __slots__ = ["name", "publisher", "episode_count", "uri"]

    def __init__(self, name, publisher, episode_count, uri):
        self.name = name
        self.publisher = publisher
        self.episode_count = episode_count
        self.uri = uri

    def __str__(self):
        return self.name + " - " + self.publisher


class UserArtist:
    __slots__ = ["name", "uri"]

    def __init__(self, name, uri):
        self.name = name
        self.uri = uri

    def __str__(self):
        return self.name


class UserPlaylist:
    __slots__ = ["name", "idx", "uri", "track_count"]

    def __init__(self, name, idx, uri, track_count):
        self.name = name
        self.idx = idx
        self.uri = uri
        self.track_count = track_count

    def __str__(self):
        return self.name


class SearchResults:
    __slots__ = ["tracks", "artists", "albums", "album_track_map"]

    def __init__(self, tracks, artists, albums, album_track_map):
        self.tracks = tracks
        self.artists = artists
        self.albums = albums
        self.album_track_map = album_track_map


scope = (
    "user-follow-read,"
    "user-library-read,"
    "user-library-modify,"
    "user-read-recently-played,"
    "user-top-read,"
    "playlist-read-private,"
    "playlist-read-collaborative,"
    "playlist-modify-public,"
    "playlist-modify-private"
)

DATA_DIR = os.environ.get(
    "SPOTIFYPOD_DATA_DIR",
    os.path.expanduser("~/.local/share/spotifypod"),
)
TOKEN_PATH = os.environ.get(
    "SPOTIFYPOD_TOKEN_PATH",
    os.path.join(DATA_DIR, "token.json"),
)
os.makedirs(os.path.dirname(TOKEN_PATH) or ".", exist_ok=True)

DATASTORE = datastore.Datastore()

pageSize = 50
has_internet = False
sp: Optional[spotipy.Spotify] = None
_sync_progress = {"state": "idle", "message": "", "pct": 0}
sleep_time = 0.3


def token_path() -> str:
    return TOKEN_PATH


def has_token() -> bool:
    return os.path.isfile(TOKEN_PATH) and os.path.getsize(TOKEN_PATH) > 10


PKCE_VERIFIER_PATH = os.path.join(os.path.dirname(TOKEN_PATH), "pkce_verifier")


def get_auth_manager() -> SpotifyPKCE:
    client_id = os.environ.get("SPOTIPY_CLIENT_ID", "")
    redirect_uri = os.environ.get(
        "SPOTIPY_REDIRECT_URI",
        "http://127.0.0.1:8080/callback",
    )
    return SpotifyPKCE(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        cache_path=TOKEN_PATH,
        open_browser=False,
    )


def begin_pkce_login(state: Optional[str] = None) -> str:
    """Return the Spotify authorize URL and persist the PKCE verifier.

    The authorize and callback HTTP requests are handled by different
    SpotifyPKCE instances (and possibly different processes), so the
    code_verifier generated here must survive until the code exchange.
    """
    auth = get_auth_manager()
    url = auth.get_authorize_url(state=state)
    os.makedirs(os.path.dirname(PKCE_VERIFIER_PATH), exist_ok=True)
    fd = os.open(PKCE_VERIFIER_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(auth.code_verifier)
    return url


def finish_pkce_login(code: str) -> None:
    """Exchange the authorization code using the verifier saved by begin_pkce_login."""
    auth = get_auth_manager()
    try:
        with open(PKCE_VERIFIER_PATH) as fh:
            verifier = fh.read().strip()
    except OSError:
        verifier = ""
    if not verifier:
        raise RuntimeError(
            "No pending login: start again with 'Authorize with Spotify' and complete it in one go."
        )
    # Both must be set, otherwise spotipy silently generates a fresh pair
    # (-> "code_verifier was incorrect").
    auth.code_verifier = verifier
    auth.code_challenge = auth._get_code_challenge()
    auth.get_access_token(code=code, check_cache=False)
    try:
        os.remove(PKCE_VERIFIER_PATH)
    except OSError:
        pass


def init_spotify(force: bool = False) -> Optional[spotipy.Spotify]:
    global sp
    if sp is not None and not force:
        return sp
    if not has_token() and not os.environ.get("SPOTIPY_CLIENT_ID"):
        return None
    try:
        auth = get_auth_manager()
        # Validate / refresh token if cache exists
        if has_token():
            token = auth.get_cached_token()
            if not token:
                # try refresh via validate
                try:
                    auth.validate_token(auth.get_access_token(check_cache=True))
                except Exception:
                    pass
        sp = spotipy.Spotify(auth_manager=auth)
        return sp
    except Exception as exc:
        print("init_spotify failed:", exc)
        sp = None
        return None


def check_internet(request):
    global has_internet
    try:
        result = request()
        has_internet = True
        return result
    except Exception:
        print("no ints")
        has_internet = False
        return None


def _client() -> spotipy.Spotify:
    client = init_spotify()
    if client is None:
        raise RuntimeError("Spotify Web API not authenticated")
    return client


def get_playlist(id):
    results = _client().playlist(id)
    tracks = []
    items = results.get("items") or results.get("tracks", {}).get("items") or []
    # playlist() may still nest under items/tracks depending on API version
    if not items and "tracks" in results:
        items = results["tracks"].get("items") or []
    for item in items:
        track = item.get("item") or item.get("track")
        if not track or track.get("is_local"):
            continue
        artists = track.get("artists") or [{"name": "?"}]
        album = track.get("album") or {"name": "?"}
        tracks.append(
            UserTrack(track["name"], artists[0]["name"], album["name"], track["uri"])
        )
    return (
        UserPlaylist(results["name"], 0, results["uri"], len(tracks)),
        tracks,
    )


def get_show(id):
    results = _client().show(id)
    show = results["name"]
    publisher = results["publisher"]
    episodes = []
    for item in results.get("episodes", {}).get("items") or []:
        episodes.append(UserEpisode(item["name"], publisher, show, item["uri"]))
    return (UserShow(results["name"], publisher, len(episodes), results["uri"]), episodes)


def get_album(id):
    results = _client().album(id)
    album = results["name"]
    artist = results["artists"][0]["name"]
    tracks = []
    for item in results.get("tracks", {}).get("items") or []:
        tracks.append(UserTrack(item["name"], artist, album, item["uri"]))
    return (UserAlbum(results["name"], artist, len(tracks), results["uri"]), tracks)


def _iter_playlist_items(playlist_id: str):
    """Yield track dicts from playlist_items; empty on 403 (non-owned)."""
    try:
        results = _client().playlist_items(playlist_id, limit=pageSize)
    except spotipy.SpotifyException as exc:
        if exc.http_status == 403:
            print(f"playlist {playlist_id}: 403 (not owned) — play-only")
            return
        raise
    while True:
        for item in results.get("items") or []:
            track = item.get("item") or item.get("track")
            if track and track.get("type") == "track" and not track.get("is_local"):
                yield track
            elif track and track.get("type") == "episode":
                # skip episodes in music playlists for now
                continue
        if results.get("next"):
            results = _client().next(results)
        else:
            break


def get_playlist_tracks(id):
    tracks = []
    for track in _iter_playlist_items(id):
        artists = track.get("artists") or [{"name": "?"}]
        album = track.get("album") or {"name": "?"}
        tracks.append(
            UserTrack(track["name"], artists[0]["name"], album["name"], track["uri"])
        )
    return tracks


def get_album_tracks(id):
    album, tracks = get_album(id)
    return tracks


def ensure_playlist_tracks(playlist) -> list:
    """Lazy-load playlist tracks into the datastore."""
    cached = DATASTORE.getPlaylistTracks(playlist.uri)
    if cached is not None:
        return cached
    tracks = get_playlist_tracks(playlist.uri.split(":")[-1])
    DATASTORE.setPlaylist(playlist, tracks, index=playlist.idx if hasattr(playlist, "idx") else -1)
    # Update track_count if we discovered items
    if tracks:
        playlist.track_count = len(tracks)
    return tracks


def refresh_devices():
    """No-op: playback device is local go-librespot, not Web API devices."""
    return


def parse_album(album):
    artist = album["artists"][0]["name"]
    tracks = []
    if "tracks" not in album:
        return get_album(album["id"])
    for track in album["tracks"]["items"]:
        tracks.append(UserTrack(track["name"], artist, album["name"], track["uri"]))
    return (UserAlbum(album["name"], artist, len(tracks), album["uri"]), tracks)


def parse_show(show):
    publisher = show["publisher"]
    episodes = []
    if "episodes" not in show:
        return get_show(show["id"])
    for episode in show["episodes"]["items"]:
        episodes.append(
            UserEpisode(episode["name"], publisher, show["name"], episode["uri"])
        )
    return (UserShow(show["name"], publisher, len(episodes), show["uri"]), episodes)


def sync_progress():
    return dict(_sync_progress)


def _set_sync(state, message="", pct=0):
    _sync_progress["state"] = state
    _sync_progress["message"] = message
    _sync_progress["pct"] = pct


def refresh_data(full: bool = True):
    """
    Sync library metadata. Playlist/album *items* are fetched lazily by default
    (metadata + empty track lists) to stay within Development Mode quota.
    Pass full=True from the portal "Sync now" to also pull playlist tracks.
    """
    if init_spotify() is None:
        _set_sync("error", "Not authenticated")
        return
    _set_sync("running", "Starting…", 0)
    DATASTORE.clear()
    client = _client()

    try:
        _set_sync("running", "Saved tracks…", 5)
        results = client.current_user_saved_tracks(limit=pageSize, offset=0)
        while True:
            offset = results.get("offset", 0)
            for idx, item in enumerate(results["items"]):
                track = item["track"]
                DATASTORE.setSavedTrack(
                    idx + offset,
                    UserTrack(
                        track["name"],
                        track["artists"][0]["name"],
                        track["album"]["name"],
                        track["uri"],
                    ),
                )
            if results.get("next"):
                results = client.next(results)
            else:
                break
        print("Spotify tracks fetched")

        _set_sync("running", "Artists…", 20)
        offset = 0
        results = client.current_user_followed_artists(limit=pageSize)
        while True:
            for idx, item in enumerate(results["artists"]["items"]):
                DATASTORE.setArtist(idx + offset, UserArtist(item["name"], item["uri"]))
            if results["artists"].get("next"):
                results = client.next(results["artists"])
                offset += pageSize
            else:
                break
        print("Spotify artists fetched:", DATASTORE.getArtistCount())

        _set_sync("running", "Playlists…", 40)
        results = client.current_user_playlists(limit=pageSize)
        totalindex = 0
        while True:
            offset = results.get("offset", 0)
            for idx, item in enumerate(results["items"]):
                track_count = item.get("tracks", {}).get("total") or 0
                pl = UserPlaylist(item["name"], totalindex, item["uri"], track_count)
                if full:
                    tracks = get_playlist_tracks(item["id"])
                    if tracks:
                        pl.track_count = len(tracks)
                    DATASTORE.setPlaylist(pl, tracks, index=idx + offset)
                else:
                    DATASTORE.setPlaylist(pl, None, index=idx + offset)
                totalindex += 1
            if results.get("next"):
                results = client.next(results)
            else:
                break
        print("Spotify playlists fetched:", DATASTORE.getPlaylistCount())

        _set_sync("running", "Albums…", 65)
        results = client.current_user_saved_albums(limit=pageSize)
        while True:
            offset = results.get("offset", 0)
            for idx, item in enumerate(results["items"]):
                album, tracks = parse_album(item["album"])
                if not full:
                    tracks = tracks  # album responses usually include tracks
                DATASTORE.setAlbum(album, tracks, index=idx + offset)
            if results.get("next"):
                results = client.next(results)
            else:
                break
        print("Refreshed user albums")

        _set_sync("running", "Recently played…", 80)
        try:
            results = client.current_user_recently_played(limit=50)
            for idx, item in enumerate(results.get("items") or []):
                track = item["track"]
                DATASTORE.setRecentlyPlayed(
                    UserTrack(
                        track["name"],
                        track["artists"][0]["name"],
                        track["album"]["name"],
                        track["uri"],
                    ),
                    index=idx,
                )
        except Exception as exc:
            print("recently played failed:", exc)

        _set_sync("running", "Podcasts…", 90)
        results = client.current_user_saved_shows(limit=pageSize)
        if results.get("items"):
            for idx, item in enumerate(results["items"]):
                show, episodes = parse_show(item["show"])
                DATASTORE.setShow(show, episodes, index=idx)
        print("Spotify Shows fetched")

        _set_sync("done", "Library synced", 100)
    except Exception as exc:
        print("refresh_data error:", exc)
        _set_sync("error", str(exc), 0)


def play_artist(artist_uri, device_id=None):
    try:
        player.play(artist_uri)
    except player.PlayerError as exc:
        print("play_artist:", exc)
    refresh_now_playing()


def play_track(track_uri, device_id=None):
    try:
        player.play(track_uri)
    except player.PlayerError as exc:
        print("play_track:", exc)
    refresh_now_playing()


def play_episode(episode_uri, device_id=None):
    try:
        player.play(episode_uri)
    except player.PlayerError as exc:
        print("play_episode:", exc)
    refresh_now_playing()


def play_from_playlist(playist_uri, track_uri, device_id=None):
    print("playing ", playist_uri, track_uri)
    try:
        player.play(playist_uri, skip_to_uri=track_uri)
    except player.PlayerError as exc:
        print("play_from_playlist:", exc)
        # Fallback: play the track alone
        try:
            player.play(track_uri)
        except player.PlayerError as exc2:
            print(exc2)
    refresh_now_playing()


def play_from_show(show_uri, episode_uri, device_id=None):
    print("playing ", show_uri, episode_uri)
    try:
        player.play(show_uri, skip_to_uri=episode_uri)
    except player.PlayerError as exc:
        print("play_from_show:", exc)
        try:
            player.play(episode_uri)
        except player.PlayerError as exc2:
            print(exc2)
    refresh_now_playing()


def _status_to_now_playing(st: dict) -> Optional[dict]:
    if not st:
        return None
    # go-librespot /status shapes differ slightly by version; be defensive.
    track = st.get("track") or st.get("metadata") or {}
    uri = track.get("uri") or st.get("uri")
    name = track.get("name") or st.get("track_name") or st.get("name")
    if not name and not uri:
        # Flat fields from websocket-style status
        if not st.get("track_name") and not st.get("album_name"):
            # Some versions nest under player
            player_st = st.get("player") or {}
            track = player_st.get("track") or track
            uri = track.get("uri") or uri
            name = track.get("name") or name
    if not name and not uri:
        return None

    artists = track.get("artist_names") or track.get("artists") or []
    if artists and isinstance(artists[0], dict):
        artist = artists[0].get("name", "?")
    elif artists:
        artist = artists[0]
    else:
        artist = st.get("artist") or "?"

    album = track.get("album_name") or track.get("album") or st.get("album") or ""
    if isinstance(album, dict):
        album = album.get("name", "")

    duration = (
        track.get("duration")
        or track.get("duration_ms")
        or st.get("duration")
        or st.get("track_duration")
        or 0
    )
    progress = st.get("position") or st.get("progress") or st.get("track_position") or 0
    paused = st.get("paused")
    if paused is None:
        is_playing = bool(st.get("playing") or st.get("is_playing"))
    else:
        is_playing = not paused

    context_uri = st.get("context_uri") or track.get("context_uri")
    now_playing = {
        "name": name or "Unknown",
        "track_uri": uri or "",
        "artist": artist,
        "album": album or "",
        "duration": int(duration),
        "is_playing": is_playing,
        "progress": int(progress),
        "context_name": artist,
        "track_index": -1,
        "timestamp": time.time(),
    }

    if context_uri and "playlist" in context_uri:
        playlist = DATASTORE.getPlaylistUri(context_uri)
        tracks = DATASTORE.getPlaylistTracks(context_uri)
        if playlist and tracks and uri:
            try:
                now_playing["track_index"] = (
                    next(i for i, val in enumerate(tracks) if val.uri == uri) + 1
                )
                now_playing["track_total"] = len(tracks)
                now_playing["context_name"] = playlist.name
            except StopIteration:
                pass
    elif context_uri and "album" in context_uri:
        album_obj = DATASTORE.getAlbumUri(context_uri)
        tracks = DATASTORE.getPlaylistTracks(context_uri)
        if album_obj and tracks and uri:
            try:
                now_playing["track_index"] = (
                    next(i for i, val in enumerate(tracks) if val.uri == uri) + 1
                )
                now_playing["track_total"] = len(tracks)
                now_playing["context_name"] = album_obj.name
            except StopIteration:
                pass
    return now_playing


def get_now_playing():
    st = player.status()
    if st:
        global has_internet
        has_internet = True
        return _status_to_now_playing(st)
    return None


def search(query):
    client = _client()
    limit = 5  # API max for search is now 10; keep UI small
    track_results = client.search(query, limit=limit, type="track")
    tracks = []
    for item in track_results["tracks"]["items"]:
        tracks.append(
            UserTrack(
                item["name"],
                item["artists"][0]["name"],
                item["album"]["name"],
                item["uri"],
            )
        )
    artist_results = client.search(query, limit=limit, type="artist")
    artists = []
    for item in artist_results["artists"]["items"]:
        artists.append(UserArtist(item["name"], item["uri"]))
    album_results = client.search(query, limit=limit, type="album")
    albums = []
    album_track_map = {}
    for item in album_results["albums"]["items"]:
        album, album_tracks = parse_album(item)
        albums.append(album)
        album_track_map[album.uri] = album_tracks
    return SearchResults(tracks, artists, albums, album_track_map)


def refresh_now_playing():
    DATASTORE.now_playing = get_now_playing()


def play_next():
    global sleep_time
    try:
        player.next_track()
    except player.PlayerError as exc:
        print(exc)
    sleep_time = 0.4
    refresh_now_playing()


def play_previous():
    global sleep_time
    try:
        player.previous_track()
    except player.PlayerError as exc:
        print(exc)
    sleep_time = 0.4
    refresh_now_playing()


def pause():
    global sleep_time
    try:
        player.pause()
    except player.PlayerError as exc:
        print(exc)
    sleep_time = 0.4
    refresh_now_playing()


def resume():
    global sleep_time
    try:
        player.resume()
    except player.PlayerError as exc:
        print(exc)
    sleep_time = 0.4
    refresh_now_playing()


def toggle_play():
    now_playing = DATASTORE.now_playing
    try:
        player.playpause()
    except player.PlayerError:
        if not now_playing:
            return
        if now_playing.get("is_playing"):
            pause()
        else:
            resume()
        return
    sleep_time = 0.4
    refresh_now_playing()


def bg_loop():
    global sleep_time
    while True:
        refresh_now_playing()
        time.sleep(sleep_time)
        sleep_time = min(4, sleep_time * 2)


thread = threading.Thread(target=bg_loop, args=(), daemon=True)
thread.start()


def run_async(fun):
    threading.Thread(target=fun, args=(), daemon=True).start()


# Eager init if token already present
init_spotify()
