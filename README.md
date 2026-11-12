# `rive-radar` — `*.rive.to` Ecosystem Discovery & Offline Bundler

Discovery, deduplication, HTTP/asset liveness registry, and zero-internet offline bundler for hosted `*.rive.to` Rive interactive apps.

## Directory Structure

- `scripts/discover_rive.py`: Queries strictly public endpoints (`rive.to`, `urlscan.io`, `web.archive.org` CDX, `hn.algolia.com`, Rive Framer search index, `rive.app/community`, `rive.app/marketplace`, GitHub repositories search, `crt.sh`), deduplicates `*.rive.to` hosts, and probes HTTP status, `<title>`, `data-runtime`, `/file.riv` Content-Length, and `/poster.webp`.
- `scripts/bundle_rive.py`: Captures and offline-bundles all alive `/file.riv` apps from `data/rive_catalog.json` into `G:\site-matrix\rive.site\`. Downloads exact `@rive-app/webgl2` / `@rive-app/webgl2-threads` JS + WASM assets into `rive.site/_shared_runtime/` (hardlinked into each app's `_player/runtime/`), patches `/_v` telemetry and CDN script loaders, and generates standalone `server.py`, `start.bat`, and `ORIGIN.md` per app.
- `data/rive_catalog.json`: Structured JSON catalog of source yields, probed host records, and bundle status (`49/49` alive `/file.riv` hosts bundled).
- `reports/RIVE_DISCOVERY_REPORT.md`: Human-readable discovery and liveness summary report.

## Usage

### 1. Discover & Probe Live Hosts

```powershell
python scripts/discover_rive.py --proxy http://127.0.0.1:17890
```

### 2. Offline-Bundle All Alive Apps

```powershell
python scripts/bundle_rive.py --proxy http://127.0.0.1:17890 --workers 6
```

Pass `--no-proxy` to run direct without an HTTP(S) proxy, `--slugs <slug ...>` to bundle specific apps, or `--force` to re-bundle existing apps.
