"""Setup / first-boot state machine for the iPod UI."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import player
import spotify_manager


class SetupPhase(Enum):
    WIFI = "wifi"
    CONNECT = "connect"
    LINK = "link"
    SYNC = "sync"
    READY = "ready"


@dataclass
class SetupStatus:
    phase: SetupPhase
    title: str
    detail: str
    qr_payload: Optional[str] = None
    progress: int = 0


def _run(cmd: list[str], timeout: float = 2.0) -> str:
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=timeout)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def wifi_online() -> bool:
    # Prefer NetworkManager
    state = _run(["nmcli", "-t", "-f", "STATE", "g"])
    if state:
        return state.lower() in ("connected", "connected (site)", "connected (local only)") or "connected" in state.lower()
    # Fallback: default route
    return bool(_run(["ip", "route", "show", "default"]))


def hotspot_ssid() -> Optional[str]:
    """Best-effort comitup / AP SSID detection."""
    if shutil.which("comitup-cli"):
        info = _run(["comitup-cli", "i"])
        # Typical: state HOTSPOT / ssid comitup-nnn
        for line in info.splitlines():
            low = line.lower()
            if "ssid" in low or "hotspot" in low:
                parts = line.replace("=", " ").split()
                for p in parts:
                    if p.startswith("iPod-") or p.startswith("comitup-"):
                        return p
        # Try state line
        st = _run(["comitup-cli", "state"])
        if "HOTSPOT" in st.upper():
            # Parse from iw or nmcli
            pass
    # nmcli active AP mode connections
    out = _run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "con", "show", "--active"])
    for line in out.splitlines():
        if "hotspot" in line.lower() or "wifi-ap" in line.lower():
            return line.split(":")[0]
    # Scanning for iPod-* / comitup-* AP we are hosting
    out = _run(["nmcli", "-t", "-f", "NAME", "con", "show", "--active"])
    for name in out.splitlines():
        if name.startswith("iPod-") or name.startswith("comitup-"):
            return name
    # Last resort: hostname-based guess used by install
    return None


def portal_base_url() -> str:
    host = os.environ.get("PORTAL_HOST", "ipod.local")
    return f"http://{host}"


def poll() -> SetupStatus:
    if not wifi_online():
        ssid = hotspot_ssid() or "iPod-setup"
        # WIFI:S:<ssid>;T:nopass;;  — comitup APs are typically open
        qr = f"WIFI:S:{ssid};T:nopass;;"
        return SetupStatus(
            phase=SetupPhase.WIFI,
            title="Join Wi-Fi",
            detail=f"Scan QR or join “{ssid}”, then pick your network",
            qr_payload=qr,
        )

    if not player.logged_in():
        return SetupStatus(
            phase=SetupPhase.CONNECT,
            title="Spotify Connect",
            detail='Open Spotify → Devices → choose “iPod”',
            qr_payload=None,
        )

    if not spotify_manager.has_token():
        url = portal_base_url()
        return SetupStatus(
            phase=SetupPhase.LINK,
            title="Link library",
            detail=f"Scan QR or open {url}",
            qr_payload=url,
        )

    sync = spotify_manager.sync_progress()
    if sync.get("state") == "running":
        return SetupStatus(
            phase=SetupPhase.SYNC,
            title="Syncing",
            detail=sync.get("message") or "Syncing library…",
            progress=int(sync.get("pct") or 0),
        )

    if not spotify_manager.DATASTORE.has_library():
        # Kick off a background sync once
        if sync.get("state") not in ("running", "error"):
            spotify_manager.run_async(lambda: spotify_manager.refresh_data(full=False))
        return SetupStatus(
            phase=SetupPhase.SYNC,
            title="Syncing",
            detail="Fetching playlists…",
            progress=int(sync.get("pct") or 0),
        )

    return SetupStatus(
        phase=SetupPhase.READY,
        title="Ready",
        detail="",
        progress=100,
    )
