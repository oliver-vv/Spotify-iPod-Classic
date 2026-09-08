# Frontend (iPod UI)

Tkinter UI (ChicagoFLF look) driven by UDP click-wheel events on port **9090**.

## Modules

| File | Role |
|------|------|
| `spotifypod.py` | UI + UDP input + setup QR screens |
| `view_model.py` | Menu navigation |
| `spotify_manager.py` | Web API (spotipy PKCE) + library sync |
| `player.py` | go-librespot REST client |
| `datastore.py` | SQLite cache |
| `setup_state.py` | First-boot Wi‑Fi / Connect / link state |
| `bt_manager.py` | Bluetooth speakers (bluetoothctl + PipeWire) |
| `power.py` | Clean shutdown / reboot via logind (polkit rule) |

## Dev

```bash
pip install -r requirements.txt
python3 spotifypod.py
```

On macOS the window is 320×240; use arrow keys. Playback needs go-librespot (`GO_LIBRESPOT_URL`, default `http://127.0.0.1:3678`).

Menu / Now Playing layout inspired by Guy Dupont’s sPot UI; see the root README credits.
