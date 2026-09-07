"""
Bluetooth speaker management for the iPod.

Drives bluez through `bluetoothctl` (non-interactive mode) and inspects the
PipeWire graph through `pw-dump` / `wpctl`. Everything is best-effort: a
missing adapter or a PipeWire that is not running just yields empty results.

Used by the setup portal (pair / connect / forget) and by the UI at startup
(auto-reconnect to trusted speakers).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

# PipeWire tools need the user's runtime dir; systemd sets it via pam or Environment=
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _run(args: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, f"{args[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(args)}: timed out"
    return proc.returncode, (proc.stdout + proc.stderr)


def _btctl(*cmd: str, timeout: float = 30, agent: bool = False) -> tuple[int, str]:
    args = ["bluetoothctl"]
    if agent:
        # Speakers use "Just Works" pairing; register a no-IO agent so bluez does not
        # wait for a PIN prompt nobody can answer.
        args += ["--agent", "NoInputNoOutput"]
    args += ["--timeout", str(int(timeout))]
    args += list(cmd)
    return _run(args, timeout=timeout + 5)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def valid_mac(mac: str) -> bool:
    return bool(MAC_RE.match(mac or ""))


# --------------------------------------------------------------------------- adapter


def available() -> bool:
    rc, out = _btctl("list", timeout=5)
    return rc == 0 and "Controller" in out


def power_on() -> None:
    _btctl("power", "on", timeout=5)


# --------------------------------------------------------------------------- devices


@dataclass
class BtDevice:
    mac: str
    name: str
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    icon: str = ""
    audio: bool = False  # advertises A2DP sink / audio profile

    @property
    def label(self) -> str:
        return self.name if self.name and self.name != self.mac else self.mac


def _parse_device_lines(out: str) -> list[tuple[str, str]]:
    found = []
    for line in _strip_ansi(out).splitlines():
        m = re.match(r"^\s*Device\s+(\S+)\s*(.*)$", line)
        if m and valid_mac(m.group(1)):
            found.append((m.group(1), m.group(2).strip()))
    return found


def info(mac: str) -> Optional[BtDevice]:
    if not valid_mac(mac):
        return None
    rc, out = _btctl("info", mac, timeout=5)
    if rc != 0 or "Device" not in out:
        return None
    dev = BtDevice(mac=mac, name=mac)
    for line in _strip_ansi(out).splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            dev.name = line.split(":", 1)[1].strip()
        elif line.startswith("Alias:") and dev.name == mac:
            dev.name = line.split(":", 1)[1].strip()
        elif line.startswith("Paired:"):
            dev.paired = line.endswith("yes")
        elif line.startswith("Trusted:"):
            dev.trusted = line.endswith("yes")
        elif line.startswith("Connected:"):
            dev.connected = line.endswith("yes")
        elif line.startswith("Icon:"):
            dev.icon = line.split(":", 1)[1].strip()
        elif line.startswith("UUID:") and ("Audio Sink" in line or "Advanced Audio" in line):
            dev.audio = True
    return dev


def _device_set(filter_name: str) -> Optional[set[str]]:
    """MACs matching a bluetoothctl `devices <Filter>` (bluez >= 5.65), else None."""
    rc, out = _btctl("devices", filter_name, timeout=5)
    if rc != 0 or "Usage" in out or "Invalid" in out:
        return None
    return {mac for mac, _ in _parse_device_lines(out)}


def devices() -> list[BtDevice]:
    """All devices bluez currently knows about (paired + recently seen)."""
    rc, out = _btctl("devices", timeout=5)
    if rc != 0:
        return []
    all_devs = _parse_device_lines(out)
    paired = _device_set("Paired")
    result: list[BtDevice] = []
    if paired is None:
        # Old bluez without filters: one `info` call per device (slower)
        for mac, _ in all_devs:
            dev = info(mac)
            if dev:
                result.append(dev)
    else:
        connected = _device_set("Connected") or set()
        trusted = _device_set("Trusted") or set()
        for mac, name in all_devs:
            result.append(
                BtDevice(
                    mac=mac,
                    name=name or mac,
                    paired=mac in paired,
                    trusted=mac in trusted,
                    connected=mac in connected,
                    audio=True,  # unknown without `info`; don't hide anything
                )
            )
    # Connected first, then paired, then named discoveries, nameless last
    result.sort(
        key=lambda d: (
            not d.connected,
            not d.paired,
            not d.audio,
            d.name == d.mac,
            d.name.lower(),
        )
    )
    return result


def scan(seconds: int = 8) -> None:
    """Discover nearby devices for `seconds`. Results show up in devices()."""
    power_on()
    _btctl("scan", "on", timeout=seconds)


# --------------------------------------------------------------------------- actions


def pair_and_connect(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "Invalid address"
    power_on()
    log = []
    dev = info(mac)
    if not dev or not dev.paired:
        rc, out = _btctl("pair", mac, timeout=30, agent=True)
        log.append(out.strip())
        if rc != 0 and "AlreadyExists" not in out:
            return False, "Pairing failed. Put the speaker in pairing mode and retry.\n" + "\n".join(log)
    _btctl("trust", mac, timeout=5)
    ok, msg = connect(mac)
    log.append(msg)
    return ok, "\n".join(x for x in log if x)


def connect(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "Invalid address"
    rc, out = _btctl("connect", mac, timeout=20)
    ok = rc == 0 and "Connection successful" in out
    if ok:
        # Give PipeWire a moment to create the sink, then make it the default.
        for _ in range(10):
            time.sleep(0.5)
            sink = sink_for_mac(mac)
            if sink:
                set_default_sink(sink["id"])
                break
    return ok, _strip_ansi(out).strip()


def disconnect(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "Invalid address"
    rc, out = _btctl("disconnect", mac, timeout=10)
    return rc == 0, _strip_ansi(out).strip()


def forget(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "Invalid address"
    rc, out = _btctl("remove", mac, timeout=10)
    return rc == 0, _strip_ansi(out).strip()


def auto_connect(delay: float = 5.0) -> None:
    """Reconnect trusted, paired speakers (called in a thread at UI startup)."""
    time.sleep(delay)
    try:
        if not available():
            return
        power_on()
        for dev in devices():
            if dev.paired and dev.trusted and not dev.connected:
                connect(dev.mac)
    except Exception as exc:  # never take the UI down over Bluetooth
        print("bt auto_connect:", exc)


# --------------------------------------------------------------------------- pipewire


def audio_sinks() -> list[dict]:
    """PipeWire audio sinks: [{id, name, description, default}]."""
    rc, out = _run(["pw-dump"], timeout=10)
    if rc != 0:
        return []
    try:
        objects = json.loads(out)
    except ValueError:
        return []
    default_name = None
    sinks = []
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Metadata":
            for item in obj.get("metadata") or []:
                if item.get("key") == "default.audio.sink":
                    val = item.get("value") or {}
                    default_name = val.get("name") if isinstance(val, dict) else None
        elif obj.get("type") == "PipeWire:Interface:Node":
            props = (obj.get("info") or {}).get("props") or {}
            if props.get("media.class") == "Audio/Sink":
                sinks.append(
                    {
                        "id": obj.get("id"),
                        "name": props.get("node.name", ""),
                        "description": props.get("node.description")
                        or props.get("node.nick")
                        or props.get("node.name", ""),
                        "default": False,
                    }
                )
    for s in sinks:
        s["default"] = default_name is not None and s["name"] == default_name
    return sinks


def sink_for_mac(mac: str) -> Optional[dict]:
    key = mac.replace(":", "_").upper()
    for s in audio_sinks():
        if key in s["name"].upper():
            return s
    return None


def set_default_sink(sink_id) -> bool:
    rc, _ = _run(["wpctl", "set-default", str(sink_id)], timeout=5)
    return rc == 0


def status() -> dict:
    """Everything the portal needs in one call."""
    try:
        avail = available()
    except Exception:
        avail = False
    devs = devices() if avail else []
    try:
        sinks = audio_sinks()
    except Exception:
        sinks = []
    return {
        "available": avail,
        "devices": devs,
        "connected": [d for d in devs if d.connected],
        "sinks": sinks,
        "default_sink": next((s for s in sinks if s["default"]), None),
    }
