#!/usr/bin/env bash
# Point Xorg's fbdev driver at the SPI LCD, whatever /dev/fbN it landed on.
#
# With vc4-kms-v3d disabled the Pi firmware still creates its own framebuffer
# for the (absent) HDMI output as /dev/fb0, so the mipi-dbi panel usually
# becomes /dev/fb1. Hard-coding a number in xorg.conf is therefore fragile.
# Runs as root from spotifypod.service (ExecStartPre=+...).
set -u
CONF="${1:-/etc/X11/xorg.conf.d/99-fbdev.conf}"

find_lcd() {
  local f name
  for f in /sys/class/graphics/fb*; do
    [[ -r "${f}/name" ]] || continue
    name="$(<"${f}/name")"
    case "${name}" in
      *mipi*|*dbi*|*panel*) echo "/dev/$(basename "${f}")"; return 0 ;;
    esac
  done
  return 1
}

# The panel probes over SPI early, but give it a moment on slow boots.
pick=""
for _ in $(seq 1 20); do
  pick="$(find_lcd)" && break
  sleep 0.5
done

if [[ -z "${pick}" ]]; then
  # Fall back to the highest-numbered fb: the SPI panel registers after the firmware fb.
  last="$(ls -1 /sys/class/graphics/ 2>/dev/null | grep '^fb[0-9]' | sort -V | tail -1)"
  if [[ -n "${last}" ]]; then
    pick="/dev/${last}"
    echo "select-lcd-fb: WARN no mipi-dbi framebuffer found; falling back to ${pick}" >&2
  fi
fi

if [[ -z "${pick}" ]]; then
  echo "select-lcd-fb: no framebuffer device at all; leaving ${CONF} untouched" >&2
  exit 0
fi

if [[ -f "${CONF}" ]]; then
  sed -i "s|Option \"fbdev\" \"/dev/fb[^\"]*\"|Option \"fbdev\" \"${pick}\"|" "${CONF}"
fi
echo "select-lcd-fb: Xorg -> ${pick} ($(<"/sys/class/graphics/$(basename "${pick}")/name"))"
