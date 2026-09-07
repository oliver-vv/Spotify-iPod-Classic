"""
Bluetooth speaker management for the iPod.

Drives bluez through `bluetoothctl` (non-interactive mode) and inspects the
PipeWire graph through `pw-dump` / `wpctl`. Everything is best-effort: a
missing adapter or a PipeWire that is not running just yields empty results.

Used by the setup portal (scan / pair / connect / forget, via JSON) and by the
UI at startup (auto-reconnect to trusted speakers).

NB: `bluetoothctl --timeout N` means "keep running for N seconds" (it is meant
for `scan on`), NOT "give up after N seconds". Never pass it for one-shot
commands, or every call blocks for the full N seconds.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

# PipeWire tools need the user's runtime dir; systemd sets it via pam or Environment=
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
# bluetoothctl scan <transport>: "bredr" (classic, what speakers use), "le", or "on" (both)
SCAN_TRANSPORT = os.environ.get("BT_SCAN_TRANSPORT", "bredr")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_DEVICE_LINE_RE = re.compile(r"Device\s+(([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*)$")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _run(args: list[str], timeout: float = 10) -> tuple[int, str]:
    """Run a one-shot command; `timeout` is a hard cap, the process is killed after it."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, f"{args[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(args)}: timed out after {timeout:.0f}s"
    return proc.returncode, _strip_ansi(proc.stdout + proc.stderr)


def _btctl(*cmd: str, timeout: float = 10, agent: bool = False) -> tuple[int, str]:
    """One-shot bluetoothctl command. Exits as soon as the command completes."""
    args = ["bluetoothctl"]
    if agent:
        # Speakers use "Just Works" pairing; register a no-IO agent so bluez does not
        # wait for a PIN prompt nobody can answer.
        args += ["--agent", "NoInputNoOutput"]
    args += list(cmd)
    return _run(args, timeout=timeout)


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

    @property
    def label(self) -> str:
        return self.name if self.name and self.name != self.mac else self.mac

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        return d


def _parse_device_lines(out: str) -> list[tuple[str, str]]:
    found = []
    for line in out.splitlines():
        m = _DEVICE_LINE_RE.search(line)
        if m:
            found.append((m.group(1).upper(), m.group(3).strip()))
    return found


def info(mac: str) -> Optional[BtDevice]:
    if not valid_mac(mac):
        return None
    rc, out = _btctl("info", mac, timeout=5)
    if rc != 0 or "Device" not in out:
        return None
    dev = BtDevice(mac=mac.upper(), name=mac.upper())
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            dev.name = line.split(":", 1)[1].strip()
        elif line.startswith("Alias:") and dev.name == dev.mac:
            dev.name = line.split(":", 1)[1].strip()
        elif line.startswith("Paired:"):
            dev.paired = line.endswith("yes")
        elif line.startswith("Trusted:"):
            dev.trusted = line.endswith("yes")
        elif line.startswith("Connected:"):
            dev.connected = line.endswith("yes")
    return dev


def _device_set(filter_name: str) -> Optional[set[str]]:
    """MACs matching `bluetoothctl devices <Filter>` (bluez >= 5.65), else None."""
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
                )
            )
    return result


# --------------------------------------------------------------------------- scanning


class _Scanner:
    """Background discovery. Parses bluetoothctl's live [NEW]/[CHG] output so the
    portal can show devices while the scan is still running, and remembers names
    after bluez has expired the temporary device objects."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._seen: dict[str, str] = {}  # mac -> name (from this or earlier scans)
        self._started = 0.0

    @property
    def scanning(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start(self, seconds: int = 12) -> bool:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return False  # already scanning
            power_on()
            try:
                # "bredr": classic Bluetooth only. Speakers are BR/EDR; scanning "on"
                # (BR/EDR + LE) floods the list with nameless phones/TVs/gadgets.
                self._proc = subprocess.Popen(
                    ["bluetoothctl", "--timeout", str(int(seconds)), "scan", SCAN_TRANSPORT],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except FileNotFoundError:
                self._proc = None
                return False
            self._started = time.time()
            threading.Thread(target=self._reader, args=(self._proc,), daemon=True).start()
            return True

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _reader(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = _strip_ansi(raw).strip()
            # [NEW] Device AA:BB:.. Name       |  [CHG] Device AA:BB:.. Name: Foo
            m = _DEVICE_LINE_RE.search(line)
            if not m or "[DEL]" in line:
                continue
            mac, rest = m.group(1).upper(), m.group(3).strip()
            name = None
            if line.startswith("[NEW]") or line.startswith("Device"):
                name = rest
            elif rest.startswith("Name:") or rest.startswith("Alias:"):
                name = rest.split(":", 1)[1].strip()
            if name and name.replace("-", ":").upper() == mac:
                name = None  # bluez uses the address as a placeholder name
            with self._lock:
                if name and not name.startswith("RSSI") and not name.startswith("TxPower"):
                    self._seen[mac] = name
                else:
                    self._seen.setdefault(mac, mac)
        proc.wait()

    def seen(self) -> dict[str, str]:
        with self._lock:
            return dict(self._seen)

    def forget(self, mac: str) -> None:
        with self._lock:
            self._seen.pop(mac.upper(), None)


_scanner = _Scanner()


def start_scan(seconds: int = 12) -> bool:
    return _scanner.start(seconds)


def stop_scan() -> None:
    _scanner.stop()


def is_scanning() -> bool:
    return _scanner.scanning


def scan(seconds: int = 8) -> None:
    """Blocking discovery (used before pairing a device bluez no longer knows)."""
    power_on()
    _run(["bluetoothctl", "--timeout", str(int(seconds)), "scan", SCAN_TRANSPORT], timeout=seconds + 5)


# --------------------------------------------------------------------------- actions


def pair_and_connect(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "Invalid address"
    mac = mac.upper()
    stop_scan()  # pairing while discovering is unreliable
    power_on()
    dev = info(mac)
    if dev is None:
        # bluez expires unpaired devices ~30s after a scan; find it again first
        scan(8)
        dev = info(mac)
        if dev is None:
            return False, "Speaker not found. Make sure it is in pairing mode and scan again."
    log = []
    if not dev.paired:
        rc, out = _btctl("pair", mac, timeout=45, agent=True)
        log.append(out.strip())
        if rc != 0 and "AlreadyExists" not in out and "Pairing successful" not in out:
            return False, "Pairing failed. Put the speaker in pairing mode and retry.\n" + "\n".join(log)
    _btctl("trust", mac, timeout=5)
    ok, msg = connect(mac)
    log.append(msg)
    return ok, "\n".join(x for x in log if x)


def connect(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "Invalid address"
    rc, out = _btctl("connect", mac.upper(), timeout=25)
    ok = "Connection successful" in out or (rc == 0 and "Failed" not in out)
    if ok:
        # Give PipeWire a moment to create the sink, then make it the default.
        for _ in range(12):
            time.sleep(0.5)
            sink = sink_for_mac(mac)
            if sink:
                set_default_sink(sink["id"])
                break
    return ok, out.strip()


def disconnect(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "Invalid address"
    rc, out = _btctl("disconnect", mac.upper(), timeout=10)
    return rc == 0, out.strip()


def forget(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "Invalid address"
    rc, out = _btctl("remove", mac.upper(), timeout=10)
    _scanner.forget(mac)
    return rc == 0, out.strip()


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
    rc, out = _run(["pw-dump"], timeout=5)
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
    if sinks and not any(s["default"] for s in sinks) and len(sinks) == 1:
        sinks[0]["default"] = True  # single sink is effectively the default
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


# --------------------------------------------------------------------------- status


def status() -> dict:
    """JSON-serialisable snapshot for the portal."""
    try:
        avail = available()
    except Exception:
        avail = False
    devs: dict[str, BtDevice] = {}
    if avail:
        for d in devices():
            devs[d.mac] = d
        # Devices seen during scans that bluez may already have expired
        for mac, name in _scanner.seen().items():
            if mac in devs:
                if devs[mac].name == mac and name != mac:
                    devs[mac].name = name
            else:
                devs[mac] = BtDevice(mac=mac, name=name)
    ordered = sorted(
        devs.values(),
        key=lambda d: (not d.connected, not d.paired, d.name == d.mac, d.name.lower()),
    )
    try:
        sinks = audio_sinks()
    except Exception:
        sinks = []
    default_sink = next((s for s in sinks if s["default"]), None)
    return {
        "available": avail,
        "scanning": _scanner.scanning,
        "devices": [d.to_dict() for d in ordered],
        "connected": [d.to_dict() for d in ordered if d.connected],
        "sinks": sinks,
        "default_sink": default_sink,
    }
