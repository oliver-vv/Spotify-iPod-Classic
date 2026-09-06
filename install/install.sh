#!/usr/bin/env bash
# sPot / Spotifypod installer for Raspberry Pi OS Lite 64-bit (Bookworm+)
# Run as root on the Pi:  sudo bash install/install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- Tunables (override via env) ---
INSTALL_ROOT="${INSTALL_ROOT:-/opt/spotifypod}"
DATA_DIR="${DATA_DIR:-/var/lib/spotifypod}"
CONFIG_DIR="${CONFIG_DIR:-/etc/spotifypod}"
SPOTIFYPOD_USER="${SPOTIFYPOD_USER:-spotifypod}"
# Audio: pulseaudio (PipeWire BT default) or alsa (I2S/USB DAC)
AUDIO_BACKEND="${AUDIO_BACKEND:-pulseaudio}"
# Display GPIOs for mipi-dbi-spi (must not conflict with click.c: 23/25/26)
DC_GPIO="${DC_GPIO:-24}"
RESET_GPIO="${RESET_GPIO:-27}"
BL_GPIO="${BL_GPIO:-18}"
GO_LIBRESPOT_VERSION="${GO_LIBRESPOT_VERSION:-latest}"
HOSTNAME_VALUE="${HOSTNAME_VALUE:-ipod}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  python3 python3-venv python3-pip python3-tk python3-pil.imagetk \
  xserver-xorg-core xserver-xorg-video-fbdev xinit x11-xserver-utils \
  libpigpio-dev pigpio pigpio-tools \
  network-manager avahi-daemon \
  curl ca-certificates unzip fonts-dejavu-core \
  pipewire pipewire-audio pipewire-alsa wireplumber \
  libspa-0.2-bluetooth bluez \
  gcc make

# Comitup may need its own repo on Bookworm; try apt first.
if ! apt-get install -y comitup 2>/dev/null; then
  echo "WARN: comitup not in default apt; see https://davesteele.github.io/comitup/ for Bookworm install"
fi

echo "==> Creating user ${SPOTIFYPOD_USER}"
if ! id -u "${SPOTIFYPOD_USER}" >/dev/null 2>&1; then
  useradd --system --home "${DATA_DIR}" --shell /usr/sbin/nologin \
    --groups audio,video "${SPOTIFYPOD_USER}" || true
fi
usermod -aG audio,video "${SPOTIFYPOD_USER}" 2>/dev/null || true

echo "==> Layout under ${INSTALL_ROOT}"
mkdir -p "${INSTALL_ROOT}/bin" "${INSTALL_ROOT}/frontend" "${INSTALL_ROOT}/portal" \
  "${DATA_DIR}/go-librespot" "${CONFIG_DIR}" /lib/firmware /etc/X11/xorg.conf.d
chown -R "${SPOTIFYPOD_USER}:${SPOTIFYPOD_USER}" "${DATA_DIR}"

# Sync application code
rsync -a --delete \
  --exclude '__pycache__' --exclude '.cache' --exclude '*.pyc' --exclude '.env' \
  "${REPO_ROOT}/frontend/" "${INSTALL_ROOT}/frontend/"
rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "${REPO_ROOT}/portal/" "${INSTALL_ROOT}/portal/"

install -m 755 "${SCRIPT_DIR}/config/xinitrc" "${INSTALL_ROOT}/xinitrc"
chown -R "${SPOTIFYPOD_USER}:${SPOTIFYPOD_USER}" "${INSTALL_ROOT}/frontend" "${INSTALL_ROOT}/portal"

echo "==> Python venv"
python3 -m venv --system-site-packages "${INSTALL_ROOT}/venv"
"${INSTALL_ROOT}/venv/bin/pip" install --upgrade pip
"${INSTALL_ROOT}/venv/bin/pip" install -r "${INSTALL_ROOT}/frontend/requirements.txt"
# Portal shares the same venv
setcap 'cap_net_bind_service=+ep' "${INSTALL_ROOT}/venv/bin/python3" 2>/dev/null || \
  echo "WARN: setcap failed; portal may need to run as root or use authbind for port 80"

echo "==> Compile click wheel binary"
gcc -Wall -pthread -O2 -o "${INSTALL_ROOT}/bin/click" \
  "${REPO_ROOT}/clickwheel/click.c" -lpigpio -lrt
chmod 755 "${INSTALL_ROOT}/bin/click"

echo "==> Download go-librespot (arm64)"
ARCH="$(uname -m)"
case "${ARCH}" in
  aarch64|arm64) GL_ARCH="linux_arm64" ;;
  armv7l|armhf) GL_ARCH="linux_arm" ;;
  x86_64) GL_ARCH="linux_amd64" ;;
  *) echo "Unsupported arch ${ARCH}"; exit 1 ;;
esac

TMP="$(mktemp -d)"
if [[ "${GO_LIBRESPOT_VERSION}" == "latest" ]]; then
  API_URL="https://api.github.com/repos/devgianlu/go-librespot/releases/latest"
else
  API_URL="https://api.github.com/repos/devgianlu/go-librespot/releases/tags/${GO_LIBRESPOT_VERSION}"
fi
ASSET_URL="$(curl -fsSL "${API_URL}" | python3 -c "
import json,sys
rel=json.load(sys.stdin)
want='${GL_ARCH}'
for a in rel.get('assets',[]):
    n=a['name'].lower()
    if want.replace('_','-') in n or want in n:
        if n.endswith('.tar.gz') or n.endswith('.tgz') or n.endswith('.zip') or 'go-librespot' in n:
            print(a['browser_download_url']); break
else:
    # fallback: any asset matching arch
    for a in rel.get('assets',[]):
        if want.split('_')[-1] in a['name'].lower():
            print(a['browser_download_url']); break
")"
if [[ -z "${ASSET_URL}" ]]; then
  echo "WARN: could not resolve go-librespot asset; place binary at ${INSTALL_ROOT}/bin/go-librespot manually"
else
  echo "Downloading ${ASSET_URL}"
  curl -fsSL -o "${TMP}/gl.asset" "${ASSET_URL}"
  if file "${TMP}/gl.asset" | grep -qi 'gzip\|tar'; then
    tar -xzf "${TMP}/gl.asset" -C "${TMP}"
  elif file "${TMP}/gl.asset" | grep -qi zip; then
    unzip -o "${TMP}/gl.asset" -d "${TMP}"
  else
    cp "${TMP}/gl.asset" "${TMP}/go-librespot"
  fi
  BIN="$(find "${TMP}" -type f -name 'go-librespot' | head -1)"
  if [[ -n "${BIN}" ]]; then
    install -m 755 "${BIN}" "${INSTALL_ROOT}/bin/go-librespot"
  else
    echo "WARN: go-librespot binary not found in archive"
  fi
fi
rm -rf "${TMP}"

echo "==> go-librespot config"
sed "s/__AUDIO_BACKEND__/${AUDIO_BACKEND}/g" \
  "${SCRIPT_DIR}/config/go-librespot.yml.template" \
  > "${DATA_DIR}/go-librespot/config.yml"
chown -R "${SPOTIFYPOD_USER}:${SPOTIFYPOD_USER}" "${DATA_DIR}/go-librespot"

echo "==> Display panel.bin (ST7789V)"
PANEL_TXT="${SCRIPT_DIR}/config/panel.txt"
if command -v mipi-dbi-cmd >/dev/null 2>&1; then
  mipi-dbi-cmd /lib/firmware/panel.bin "${PANEL_TXT}"
else
  python3 "${SCRIPT_DIR}/tools/mipi_dbi_cmd.py" /lib/firmware/panel.bin "${PANEL_TXT}"
fi
# Overlay with compatible=spotifypod,st7789v looks for spotifypod,st7789v.bin
if [[ -f /lib/firmware/panel.bin ]]; then
  cp /lib/firmware/panel.bin "/lib/firmware/spotifypod,st7789v.bin" || true
fi

echo "==> Xorg fbdev config"
install -m 644 "${SCRIPT_DIR}/config/99-fbdev.conf" /etc/X11/xorg.conf.d/99-fbdev.conf

echo "==> Boot config snippet"
BOOTCFG=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
  if [[ -f "${candidate}" ]]; then BOOTCFG="${candidate}"; break; fi
done
if [[ -n "${BOOTCFG}" ]]; then
  SNIPPET="$(mktemp)"
  sed -e "s/__DC_GPIO__/${DC_GPIO}/g" \
      -e "s/__RESET_GPIO__/${RESET_GPIO}/g" \
      -e "s/__BL_GPIO__/${BL_GPIO}/g" \
      "${SCRIPT_DIR}/config/spotifypod-config.txt.snippet" > "${SNIPPET}"
  if ! grep -q 'sPot / Spotifypod display' "${BOOTCFG}"; then
    echo "" >> "${BOOTCFG}"
    cat "${SNIPPET}" >> "${BOOTCFG}"
  fi
  # Comment out vc4-kms-v3d if present (SPI-only display)
  sed -i 's/^dtoverlay=vc4-kms-v3d/# dtoverlay=vc4-kms-v3d  # disabled for SPI LCD/' "${BOOTCFG}" || true
  rm -f "${SNIPPET}"
else
  echo "WARN: no config.txt found; copy install/config/spotifypod-config.txt.snippet manually"
fi

echo "==> App config.env"
if [[ ! -f "${CONFIG_DIR}/config.env" ]]; then
  install -m 640 "${SCRIPT_DIR}/config/config.env.example" "${CONFIG_DIR}/config.env"
  echo "Edit ${CONFIG_DIR}/config.env and set SPOTIPY_CLIENT_ID / SPOTIPY_REDIRECT_URI"
fi
chown root:"${SPOTIFYPOD_USER}" "${CONFIG_DIR}/config.env"
chmod 640 "${CONFIG_DIR}/config.env"

echo "==> Comitup"
if [[ -d /etc/comitup.conf ]] || [[ -f /etc/comitup.conf ]]; then
  :
fi
if [[ -f /etc/comitup.conf ]]; then
  cp "${SCRIPT_DIR}/config/comitup.conf" /etc/comitup.conf
elif [[ -d /etc ]]; then
  install -m 644 "${SCRIPT_DIR}/config/comitup.conf" /etc/comitup.conf
fi

echo "==> Hostname / Avahi"
hostnamectl set-hostname "${HOSTNAME_VALUE}" 2>/dev/null || echo "${HOSTNAME_VALUE}" > /etc/hostname
if [[ -f /etc/avahi/avahi-daemon.conf ]]; then
  systemctl enable avahi-daemon || true
fi

echo "==> Fonts (ChicagoFLF if present in repo)"
mkdir -p /usr/local/share/fonts/spotifypod
if [[ -d "${REPO_ROOT}/fonts" ]]; then
  find "${REPO_ROOT}/fonts" -type f \( -iname '*.ttf' -o -iname '*.otf' \) \
    -exec install -m 644 {} /usr/local/share/fonts/spotifypod/ \;
  fc-cache -f || true
fi

echo "==> systemd units"
install -m 644 "${SCRIPT_DIR}/systemd/click.service" /etc/systemd/system/click.service
install -m 644 "${SCRIPT_DIR}/systemd/go-librespot.service" /etc/systemd/system/go-librespot.service
install -m 644 "${SCRIPT_DIR}/systemd/spotifypod.service" /etc/systemd/system/spotifypod.service
install -m 644 "${SCRIPT_DIR}/systemd/spotifypod-portal.service" /etc/systemd/system/spotifypod-portal.service
systemctl daemon-reload
systemctl enable pigpiod click.service go-librespot.service spotifypod.service spotifypod-portal.service
systemctl enable NetworkManager || true
systemctl enable comitup 2>/dev/null || true

# Autologin on tty1 so xinit can claim VT1 without getty conflict
mkdir -p /etc/systemd/system/carlos.r@example.net.d
cat > /etc/systemd/system/carlos.r@example.net.d/autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${SPOTIFYPOD_USER} --noclear %I \$TERM
EOF
# Prefer system spotifypod service over interactive login starting X
systemctl disable carlos.r@example.net 2>/dev/null || true

echo ""
echo "Install complete."
echo "  1. Edit ${CONFIG_DIR}/config.env (Spotify Client ID + redirect URI)"
echo "  2. Enable GitHub Pages for docs/spotify-callback.html and register that HTTPS URI"
echo "  3. Confirm display GPIOs (DC=${DC_GPIO} RESET=${RESET_GPIO} BL=${BL_GPIO})"
echo "  4. Reboot. First boot: join iPod hotspot QR / comitup, then Spotify Connect to 'iPod',"
echo "     then open http://ipod.local to link the Web API library."
echo "  AUDIO_BACKEND=${AUDIO_BACKEND}"
