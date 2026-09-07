"""
Clean shutdown / reboot for the iPod.

Runs `systemctl poweroff|reboot` as the unprivileged spotifypod user. systemctl
hands the request to logind over D-Bus, and a polkit rule installed by
install/install.sh (/etc/polkit-1/rules.d/50-spotifypod-power.rules) allows it
for that user. No sudo involved, so it also works from the portal service,
whose capability bounding set would break setuid binaries.

After `poweroff` the Pi stays halted until power is removed and re-applied.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Tuple


def _systemctl(verb: str) -> Tuple[bool, str]:
    if not shutil.which("systemctl"):
        return False, "systemctl not available on this system"
    attempts = [["systemctl", verb]]
    if shutil.which("sudo"):
        # Fallback for setups without the polkit rule (non-interactive only)
        attempts.append(["sudo", "-n", "systemctl", verb])
    errors = []
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            continue
        if proc.returncode == 0:
            return True, ""
        errors.append((proc.stderr or proc.stdout or f"exit {proc.returncode}").strip())
    # The logind/polkit error (first attempt) is the informative one
    return False, errors[0] if errors else "unknown error"


def shutdown() -> Tuple[bool, str]:
    """Power the device off cleanly. Returns (ok, error message)."""
    return _systemctl("poweroff")


def reboot() -> Tuple[bool, str]:
    return _systemctl("reboot")
