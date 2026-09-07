"""
sPot setup portal — Wi-Fi status, Spotify PKCE linking, library sync.
Runs on the Pi at http://ipod.local (port 80).
"""
from __future__ import annotations

import os
import sys
import urllib.parse

from flask import Flask, redirect, render_template, request, url_for

# Allow importing frontend modules
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_FRONTEND = os.path.join(_ROOT, "frontend")
if _FRONTEND not in sys.path:
    sys.path.insert(0, _FRONTEND)

# Load env before spotify_manager
_ENV = os.environ.get("SPOTIFYPOD_CONFIG", "/etc/spotifypod/config.env")
if os.path.isfile(_ENV):
    with open(_ENV) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

import bt_manager  # noqa: E402
import player  # noqa: E402
import setup_state  # noqa: E402
import spotify_manager  # noqa: E402

app = Flask(__name__)


def _request_base() -> str:
    # Prefer Host header so QR / phone use the IP they already hit
    host = request.host
    return f"{request.scheme}://{host}"


def _status_ctx():
    wifi = setup_state.wifi_online()
    connected = player.logged_in()
    linked = spotify_manager.has_token()
    sync = spotify_manager.sync_progress()
    library = spotify_manager.DATASTORE.has_library()
    # Bluetooth / audio state is loaded asynchronously by the page via /api/bt so
    # the page itself renders instantly.
    return {
        "wifi": wifi,
        "connect_logged_in": connected,
        "webapi_linked": linked,
        "library": library,
        "sync": sync,
        "portal_url": setup_state.portal_base_url(),
        "device_name": "iPod",
        "client_id_set": bool(os.environ.get("SPOTIPY_CLIENT_ID")),
        "redirect_uri": os.environ.get("SPOTIPY_REDIRECT_URI", ""),
    }


@app.get("/")
def index():
    return render_template("index.html", **_status_ctx())


@app.get("/spotify/start")
def spotify_start():
    if not os.environ.get("SPOTIPY_CLIENT_ID"):
        return render_template(
            "index.html",
            error="Set SPOTIPY_CLIENT_ID in /etc/spotifypod/config.env",
            **_status_ctx(),
        )
    # state carries the HTTP callback on this device so the HTTPS relay can bounce back
    local_cb = _request_base().rstrip("/") + "/spotify/callback"
    # Generates the PKCE verifier and persists it for the callback request
    url = spotify_manager.begin_pkce_login(state=local_cb)
    return redirect(url)


@app.get("/spotify/callback")
def spotify_callback():
    error = request.args.get("error")
    if error:
        return render_template("index.html", error=f"Spotify auth error: {error}", **_status_ctx())
    code = request.args.get("code")
    if not code:
        return render_template(
            "index.html",
            error="Missing code. Use Paste URL if the redirect failed.",
            **_status_ctx(),
        )
    try:
        spotify_manager.finish_pkce_login(code)
    except Exception as exc:
        return render_template("index.html", error=str(exc), **_status_ctx())
    spotify_manager.init_spotify(force=True)
    spotify_manager.run_async(lambda: spotify_manager.refresh_data(full=False))
    return redirect(url_for("index"))


@app.post("/spotify/paste")
def spotify_paste():
    raw = (request.form.get("redirect_url") or "").strip()
    if not raw:
        return render_template("index.html", error="Paste the full redirect URL", **_status_ctx())
    try:
        parsed = urllib.parse.urlparse(raw)
        qs = urllib.parse.parse_qs(parsed.query)
        code = (qs.get("code") or [None])[0]
        if not code and "code=" in raw:
            # User pasted only the query or a fragment
            qs = urllib.parse.parse_qs(raw.split("?", 1)[-1])
            code = (qs.get("code") or [None])[0]
        if not code:
            return render_template(
                "index.html", error="No code= found in that URL", **_status_ctx()
            )
        spotify_manager.finish_pkce_login(code)
        spotify_manager.init_spotify(force=True)
        spotify_manager.run_async(lambda: spotify_manager.refresh_data(full=False))
        return redirect(url_for("index"))
    except Exception as exc:
        return render_template("index.html", error=str(exc), **_status_ctx())


@app.post("/sync")
def sync_now():
    full = request.form.get("full") == "1"
    spotify_manager.run_async(lambda: spotify_manager.refresh_data(full=full))
    return redirect(url_for("index"))


# ---------------------------------------------------------------- Bluetooth (JSON API)
# All endpoints return JSON; the page drives them with fetch() so nothing reloads.


def _json_body() -> dict:
    return request.get_json(silent=True) or request.form or {}


@app.get("/api/bt")
def api_bt():
    try:
        return bt_manager.status()
    except Exception as exc:
        return {"available": False, "scanning": False, "devices": [], "connected": [],
                "sinks": [], "default_sink": None, "error": str(exc)}


@app.post("/bt/scan")
def bt_scan():
    if not bt_manager.available():
        return {"ok": False, "message": "No Bluetooth adapter found (is bluetooth.service running?)"}, 503
    seconds = int(_json_body().get("seconds") or 12)
    bt_manager.start_scan(seconds=max(3, min(seconds, 30)))
    return {"ok": True, "scanning": True}


def _bt_action(action):
    mac = str(_json_body().get("mac") or "").strip().upper()
    if not bt_manager.valid_mac(mac):
        return {"ok": False, "message": "Invalid Bluetooth address"}, 400
    try:
        ok, msg = action(mac)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}, 500
    return {"ok": ok, "message": msg}, (200 if ok else 502)


@app.post("/bt/pair")
def bt_pair():
    return _bt_action(bt_manager.pair_and_connect)


@app.post("/bt/connect")
def bt_connect():
    return _bt_action(bt_manager.connect)


@app.post("/bt/disconnect")
def bt_disconnect():
    return _bt_action(bt_manager.disconnect)


@app.post("/bt/forget")
def bt_forget():
    return _bt_action(bt_manager.forget)


@app.post("/audio/default")
def audio_default():
    sink_id = str(_json_body().get("sink") or "").strip()
    if not sink_id.isdigit() or not bt_manager.set_default_sink(sink_id):
        return {"ok": False, "message": "Could not set default audio output"}, 502
    return {"ok": True}


@app.get("/api/status")
def api_status():
    ctx = _status_ctx()
    try:
        ctx["bt"] = bt_manager.status()
    except Exception as exc:
        ctx["bt"] = {"available": False, "error": str(exc)}
    return ctx


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORTAL_PORT", "80")), debug=False)
