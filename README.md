# sPot (2026)

Spotify client for a 4th‑generation iPod Classic housing a **Raspberry Pi Zero 2 W**, Waveshare **ST7789V** SPI LCD, and the original click wheel.

This fork modernizes [dupontgu/retro-ipod-spotify-client](https://github.com/dupontgu/retro-ipod-spotify-client) for current Spotify and Raspberry Pi OS:

| Old | New |
|-----|-----|
| raspotify + username/password | [go-librespot](https://github.com/devgianlu/go-librespot) Zeroconf + persisted credentials |
| spotipy OAuth + secret | spotipy **≥ 2.26** PKCE (client ID only) |
| Redis cache | SQLite |
| fbcp-ili9341 | Kernel `mipi-dbi-spi` + X `fbdev` |
| Manual `wpa_supplicant` | [comitup](https://davesteele.github.io/comitup/) hotspot + phone Web UI |
| openbox + lightdm | Bare `xinit` (same tkinter UI look) |

Hardware guides: [Hackaday sPot](https://hackaday.io/project/177034-spot-spotify-in-a-4th-gen-ipod-2004) · [Ricardo’s Waveshare build](http://rsflightronics.com/spotifypod)

**Do not change** [`clickwheel/click.c`](clickwheel/click.c) — it bit-bangs the wheel (GPIO 23/25) and publishes **3-byte UDP packets to `127.0.0.1:9090`**.

---

## Requirements

- Raspberry Pi Zero 2 W
- Fresh **Raspberry Pi OS Lite 64-bit** (Bookworm or newer)
- Spotify **Premium** (Connect + Development Mode Web API)
- A Spotify [Developer Dashboard](https://developer.spotify.com/dashboard) app you own
- Waveshare-style **2" ST7789V 240×320 SPI** panel (Ricardo pinout by default)

---

## 1. Flash and clone

1. Flash Raspberry Pi OS Lite 64-bit with Raspberry Pi Imager (enable SSH + Wi‑Fi *or* use the first-boot hotspot later).
2. Clone this repo on the Pi:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/YOUR_USER/Spotify-iPod-Classic.git
cd Spotify-iPod-Classic
```

---

## 2. Spotify developer app

1. Create an app at https://developer.spotify.com/dashboard  
2. Enable GitHub Pages for this repo’s `/docs` folder (Settings → Pages).  
3. Add this **Redirect URI** to the Spotify app (must be HTTPS):

```text
https://YOUR_USER.github.io/Spotify-iPod-Classic/spotify-callback.html
```

4. Copy the **Client ID** (no client secret needed for PKCE).

---

## 3. Install

```bash
sudo bash install/install.sh
```

Useful overrides:

```bash
# I2S / USB DAC instead of PipeWire Bluetooth (default)
sudo AUDIO_BACKEND=alsa bash install/install.sh

# Pins. Defaults match Ricardo's Waveshare build: display DC=24 RESET=25 BL=18,
# click wheel DATA on GPIO 5 (clock 23, haptic 26). Override only what differs, e.g.:
sudo BL_GPIO="" bash install/install.sh            # backlight hard-wired to 3.3V
sudo CLICK_DATA_PIN="" RESET_GPIO=27 bash install/install.sh   # original Dupont wiring (wheel data on 25)
```

Then edit credentials:

```bash
sudo nano /etc/spotifypod/config.env
```

Set:

```bash
SPOTIPY_CLIENT_ID=xxxxxxxx
SPOTIPY_REDIRECT_URI=https://YOUR_USER.github.io/Spotify-iPod-Classic/spotify-callback.html
```

Optional: put `ChicagoFLF.ttf` in [`fonts/`](fonts/) and re-run the installer (or `fc-cache`) so menu text matches the classic look.

Reboot:

```bash
sudo reboot
```

---

## 4. First boot (phone UX)

The iPod screen walks you through setup with QR codes:

1. **Wi‑Fi** — If offline, comitup raises hotspot `iPod-<nnn>`. Scan the Wi‑Fi QR (or join manually). Captive portal at `http://10.41.0.1` lets you pick your home network.  
2. **Spotify Connect** — In the Spotify phone app, transfer playback to device **`iPod`**. Credentials persist on disk.  
3. **Link library** — Scan the QR for `http://ipod.local` (or the Pi’s IP). Tap **Authorize with Spotify**. The HTTPS GitHub Pages page relays `code` back to the Pi. If that fails, use **Paste redirect URL**.  
4. Library sync starts automatically; then the classic sPot menu appears.  
5. **Bluetooth speaker** — on `http://ipod.local`, put your speaker in pairing mode, tap **Scan for devices**, then **Pair & connect**. The speaker is trusted and reconnected automatically on later boots; PipeWire routes go-librespot's output to it.

## 5. Everyday use

- **Volume** — on the Now Playing screen, turn the wheel: clockwise is louder. The progress bar shows the volume for two seconds, then returns to the track position (like the original iPod).
- **Turning it off** — do not just cut the power: go-librespot writes its audio cache while playing and SQLite/journald write too, and a cut mid-write can corrupt the SD card. Shut down cleanly first, either from the iPod (**Settings → Shut Down → Yes**) or from `http://ipod.local` (**Power → Shut down**). Once the screen is dark the Pi is halted; it stays halted until power is removed and re-applied, so flip your power switch then. **Reboot** is next to it in both places.
- The display stays on for as long as the iPod is powered. To blank it after inactivity, set `SCREEN_TIMEOUT_SECONDS=<seconds>` in `/etc/spotifypod/config.env` (any wheel input wakes it).
- Optional hardware power button: a momentary switch between **GPIO 3 (pin 5)** and GND plus `dtoverlay=gpio-shutdown` in `config.txt` gives a clean shutdown on press and wakes the Pi from halt on the next press. GPIO 3 is unused by this build.

Services (systemd):

- `click.service` — wheel reader (root / libpigpio; built from source on Trixie, where the apt package no longer exists)  
- `go-librespot.service` — audio + Connect  
- `spotifypod.service` — tkinter UI on X  
- `spotifypod-portal.service` — setup web UI  

---

## Architecture

```
Click wheel ──UDP :9090──► spotifypod.py (tkinter)
                              │
                              ├─► go-librespot :3678  (playback / now playing)
                              ├─► Spotify Web API     (library / search, PKCE)
                              └─► SQLite cache
Phone ──QR/captive──► Flask portal :80 ──► token.json + NetworkManager
```

---

## Display notes (SPI)

Installer appends a `mipi-dbi-spi` snippet to `/boot/firmware/config.txt` and disables `vc4-kms-v3d` so the panel is `/dev/fb0`. Convert the init sequence if needed:

```bash
# From https://github.com/notro/panel-mipi-dbi/wiki
mipi-dbi-cmd /lib/firmware/panel.bin install/config/panel.txt
```

If colours/rotation are wrong, edit MADCTL in [`install/config/panel.txt`](install/config/panel.txt) (`command 0x36 …`) and rebuild `panel.bin`.

---

## Known limitations

- Spotify Development Mode: Premium owner, ≤5 users, shared API quota — prefer **Sync metadata** over constant full syncs.  
- Playlists you don’t own may be **play-only** (no track list; Web API 403).  
- Now Playing reflects **this** iPod’s go-librespot session.  
- Single Wi‑Fi radio: hotspot drops while joining your LAN (comitup handles this).  
- “New Releases” browse API was removed by Spotify (2026); menu uses **Recently Played** instead.

---

## Local UI development (macOS / desktop)

```bash
cd frontend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Optional: mock player or point GO_LIBRESPOT_URL at a running daemon
python3 spotifypod.py   # 320×240 window; arrow keys navigate
```

---

## Wiring reminder

`clickwheel/click.c` in this repo is byte-identical to upstream (`DATA_PIN 25`). The installer compiles a temporary copy with `DATA_PIN` set from `CLICK_DATA_PIN` (default `5`, Ricardo's wiring) so the source file never changes.

| Signal | BCM GPIO (Ricardo build, default) | Physical pin |
|--------|-----------------------------------|--------------|
| Wheel clock  | 23 | 16 |
| Wheel data   | 5  | 29 |
| Haptic       | 26 | 37 |
| Display DC   | 24 | 18 |
| Display RST  | 25 | 22 |
| Display BL   | 18 | 12 |
| Display VCC  | 3.3V | 17 |
| SPI MOSI / SCLK / CE0 | 10 / 11 / 8 | 19 / 23 / 24 |

The installer refuses to run if a display pin collides with a wheel pin or the SPI bus.

---

## License

Apache-2.0 (see [LICENSE](LICENSE)). Original project by Guy Dupont / community.
