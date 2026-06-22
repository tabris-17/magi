# magi

A personal, fully local web **control panel** for this machine — a hub where each
capability is a self-contained **function** picked from a GitHub-style sidebar.
It's a unified Flask host: lightweight functions are blueprints, heavier apps are
mounted unchanged as sub-apps. Everything runs locally; nothing leaves your machine
beyond the requests a function itself makes. Responsive, so it works from a phone on
the same (trusted) network too.

Open **Appearance** in the sidebar to switch Dark / Light / System theme — saved per
device (and mirrored to the host DB).

## Run it

`./magi` is the single CLI — one front door for running, deploying, and migrating:

```bash
./magi run --env dev            # dev  — werkzeug, http://127.0.0.1:8080 (this machine only)
./magi run --env prod           # prod — waitress, binds 0.0.0.0 (the Mac mini LaunchAgent)
./magi deploy [TARGET] [--env]  # push to the mini    ./magi migrate up --env prod
./magi --help                   # usage

python3 magi.py                 # dev shortcut — delegates to the same launcher
```

There is **one front end**: the magi host. (The legacy `start.sh` / `server.py`
single-file SPA has been retired.) The only separate process is betelgeuse's
background **worker** (`functions/betelgeuse/worker.py`), run on its own.

Requirements: `pip install -r requirements.txt` **plus** each function's deps
(`pip install -r functions/betelgeuse/requirements.txt`). `ffmpeg` is needed for the
YouTube function.

### dev / prod mode

The host runs in `dev` or `prod`, from `MAGI_ENV` (set by `./magi run --env …`;
defaults to `dev` for the `python3 magi.py` shortcut). The sidebar shows a
`🐱 dev` / `🐱 prod` chip, and **dev wears a red brand** (a red `M` avatar + red
version label) so you can't mistake a dev tab for prod.

### Versions

The sidebar footer shows the **host/shell** version (`magi-1.0.0`). Each function
carries its own, shown on its dashboard card and in `/api/settings`:

- YouTube Downloader → `yd-1.0.0`
- Betelgeuse → `betelgeuse-app-<x>` (the web app) · `betelgeuse-server-<x>` (the worker)

## Functions

### 🎬 YouTube Downloader  (`/youtube/`)

Paste a URL, see **every** available format (resolutions up to 4K/8K, separate audio
tracks, codecs, sizes), pick one, and it downloads locally with a live progress bar
(streamed over Server-Sent Events). Quick picks for best video / best audio / MP3;
auto-merges video-only + audio with ffmpeg. Save location is editable per download
(defaults to a hardcoded dir); a date prefix and a `video-links.txt` metadata log are
on by default. Nothing leaves your computer except the request to YouTube itself.

### 📈 Betelgeuse  (`/betelgeuse/`)

A stock & crypto portfolio tracker with custom technical indicators, mounted as a
sub-app. It keeps its own `core/`, `worker.py`, `migrations/`, and `data/` inside
`functions/betelgeuse/`; only the shell + theme + settings are shared. Its dark mode
matches the magi shell's GitHub-dark palette.

## Architecture & deploy

See **`CLAUDE.md`** for the full architecture (host vs. functions, the function
contract, shared settings, theming) and **`deploy/README.md`** for the deploy-all /
deploy-one workflow (`./magi deploy [all|host|youtube|betelgeuse] [--env dev|prod]`,
wrapping `deploy/deploy.sh`).

## Accessing from your phone

The dev host binds to `127.0.0.1` (this machine only). `serve.py` binds `0.0.0.0` for
LAN access — only do that on a network you trust, as it exposes the panel to the LAN.
```
