"""Thin HTTP client for go-librespot local REST API (default 127.0.0.1:3678)."""
from __future__ import annotations

import os
from typing import Any, Optional

import requests

BASE_URL = os.environ.get("GO_LIBRESPOT_URL", "http://127.0.0.1:3678").rstrip("/")
TIMEOUT = float(os.environ.get("GO_LIBRESPOT_TIMEOUT", "3"))


class PlayerError(Exception):
    pass


def _url(path: str) -> str:
    return f"{BASE_URL}{path}"


def _request(method: str, path: str, json_body: Optional[dict] = None) -> Any:
    try:
        resp = requests.request(
            method,
            _url(path),
            json=json_body,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise PlayerError(str(exc)) from exc
    if resp.status_code == 204:
        return None
    if resp.status_code >= 400:
        raise PlayerError(f"{method} {path}: {resp.status_code} {resp.text[:200]}")
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def status() -> Optional[dict]:
    """Return player status dict, or None if unreachable / empty."""
    try:
        return _request("GET", "/status")
    except PlayerError:
        return None


def logged_in() -> bool:
    """True once go-librespot has an authenticated Spotify session."""
    st = status()
    if not st:
        return False
    # Persistent Zeroconf credentials / active session markers vary by version.
    if st.get("username") or st.get("user") or st.get("device_id"):
        # Prefer explicit "has track or stopped but authenticated"
        if "stopped" in st or "track" in st or "player" in st:
            return True
        return True
    # Fallback: /token returns 200 when session exists
    try:
        _request("POST", "/token")
        return True
    except PlayerError:
        return False


def play(uri: str, skip_to_uri: Optional[str] = None, paused: bool = False) -> None:
    body: dict[str, Any] = {"uri": uri, "paused": paused}
    if skip_to_uri:
        body["skip_to_uri"] = skip_to_uri
    _request("POST", "/player/play", body)


def play_uris(uris: list[str], paused: bool = False) -> None:
    if not uris:
        return
    # Play first URI as a single-track context; queue the rest when supported.
    play(uris[0], paused=paused)
    for extra in uris[1:]:
        try:
            _request("POST", "/player/add_to_queue", {"uri": extra})
        except PlayerError:
            break


def next_track() -> None:
    _request("POST", "/player/next")


def previous_track() -> None:
    _request("POST", "/player/prev")


def pause() -> None:
    _request("POST", "/player/pause")


def resume() -> None:
    _request("POST", "/player/resume")


def playpause() -> None:
    _request("POST", "/player/playpause")


def stop() -> None:
    _request("POST", "/player/stop")


def volume(value: Optional[int] = None) -> Any:
    if value is None:
        return status()
    return _request("POST", "/player/volume", {"value": value})
