# Spotify OAuth HTTPS relay (GitHub Pages)

Deploy this folder as GitHub Pages (Settings → Pages → Deploy from branch → `/docs`).

Then register this Redirect URI on your Spotify developer app:

```text
https://oliver-vv.github.io/Spotify-iPod-Classic/spotify-callback.html
```

If you rename the GitHub user or repo, update that URL in the Spotify dashboard and in `/etc/spotifypod/config.env` (`SPOTIPY_REDIRECT_URI`).
