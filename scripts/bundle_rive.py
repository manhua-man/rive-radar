#!/usr/bin/env python3
"""Capture and offline-bundle *.rive.to interactive Rive apps into G:\\site-matrix\\rive.site.

Reads G:\\site-matrix\\rive-radar\\data\\rive_catalog.json, downloads each alive app's
HTML, .riv payload, /_player/* assets, optional poster.webp / embed.html, and
exact @rive-app runtime JS + WASM files into _shared_runtime (hardlinked into each
app's _player/runtime/), patches telemetry and CDN loaders for zero-internet
execution, and writes standalone server.py, start.bat, and ORIGIN.md per app.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

RADAR_ROOT = Path(__file__).resolve().parent.parent
MATRIX_ROOT = RADAR_ROOT.parent
DEFAULT_CATALOG = RADAR_ROOT / "data" / "rive_catalog.json"
DEFAULT_OUT_DIR = MATRIX_ROOT / "rive.site"
DEFAULT_PROXY = "http://127.0.0.1:17890"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CDN_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\bsrc=["\']https://cdn\.jsdelivr\.net/npm/@rive-app/(webgl2(?:-threads)?)@([^/"\']+)/rive\.js["\'][^>]*>\s*</script>',
    re.IGNORECASE,
)
PRECONNECT_RE = re.compile(
    r'[ \t]*<link\b[^>]*\brel=["\']preconnect["\'][^>]*https://(?:cdn\.jsdelivr\.net|unpkg\.com)[^>]*>\s*\r?\n?',
    re.IGNORECASE,
)
ROOT_ATTR_RE = re.compile(
    r'\b(href|src|data-src)=(["\'])/(?!/)([^"\']*)\2',
    re.IGNORECASE,
)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

_CACHE_LOCK = threading.Lock()
_RESOLVED_VERSIONS: dict[tuple[str, str], str] = {}
_DOWNLOADED_RUNTIMES: set[tuple[str, str]] = set()


def build_opener(proxy_url: str | None) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener


def fetch_bytes(
    opener: urllib.request.OpenerDirector,
    url: str,
    timeout: int = 30,
    retries: int = 3,
    allow_404: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    last_exc: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, headers, data
        except urllib.error.HTTPError as exc:
            if allow_404 and exc.code == 404:
                return 404, {}, b""
            last_exc = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    assert last_exc is not None
    raise last_exc


def stream_to_file(
    opener: urllib.request.OpenerDirector,
    url: str,
    dest: Path,
    timeout: int = 180,
    retries: int = 3,
    expected_bytes: int | None = None,
) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest.with_suffix(dest.suffix + ".part")
    last_exc: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(req, timeout=timeout) as resp:
                cl_hdr = resp.headers.get("Content-Length")
                content_len = int(cl_hdr) if cl_hdr and cl_hdr.isdigit() else expected_bytes
                written = 0
                with open(part_path, "wb") as f:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                if content_len is not None and written != content_len:
                    raise IOError(
                        f"Incomplete download for {url}: got {written} bytes, expected {content_len}"
                    )
                os.replace(part_path, dest)
                return written
        except Exception as exc:
            last_exc = exc
            if part_path.exists():
                part_path.unlink(missing_ok=True)
            if attempt + 1 < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise
    assert last_exc is not None
    raise last_exc


def resolve_runtime_version(
    opener: urllib.request.OpenerDirector,
    pkg: str,
    spec: str,
) -> str:
    if SEMVER_RE.match(spec):
        return spec
    key = (pkg, spec)
    with _CACHE_LOCK:
        if key in _RESOLVED_VERSIONS:
            return _RESOLVED_VERSIONS[key]
    url = f"https://cdn.jsdelivr.net/npm/@rive-app/{pkg}@{spec}/package.json"
    _, _, raw = fetch_bytes(opener, url, timeout=30, retries=3)
    meta = json.loads(raw.decode("utf-8"))
    version = meta["version"]
    if not SEMVER_RE.match(version):
        raise ValueError(f"Unexpected resolved version {version!r} for {pkg}@{spec}")
    with _CACHE_LOCK:
        _RESOLVED_VERSIONS[key] = version
    return version


def ensure_shared_runtime(
    opener: urllib.request.OpenerDirector,
    shared_root: Path,
    pkg: str,
    version: str,
) -> Path:
    rt_dir = shared_root / f"{pkg}@{version}"
    files = ["rive.js", "rive.wasm", "rive_fallback.wasm"]
    if pkg == "webgl2-threads":
        files.append("rive_threads.wasm")

    with _CACHE_LOCK:
        if (pkg, version) in _DOWNLOADED_RUNTIMES and all((rt_dir / f).is_file() for f in files):
            return rt_dir
        rt_dir.mkdir(parents=True, exist_ok=True)
        for fname in files:
            dest = rt_dir / fname
            if dest.is_file() and dest.stat().st_size > 0:
                continue
            cdn_url = f"https://cdn.jsdelivr.net/npm/@rive-app/{pkg}@{version}/{fname}"
            stream_to_file(opener, cdn_url, dest, timeout=90, retries=3)
        _DOWNLOADED_RUNTIMES.add((pkg, version))
    return rt_dir


def link_or_copy_runtime(shared_rt_dir: Path, app_rt_dir: Path) -> None:
    app_rt_dir.mkdir(parents=True, exist_ok=True)
    for src in shared_rt_dir.iterdir():
        if not src.is_file():
            continue
        dst = app_rt_dir / src.name
        if dst.exists():
            dst.unlink()
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)


def patch_html(
    opener: urllib.request.OpenerDirector,
    html_text: str,
) -> tuple[str, list[tuple[str, str]]]:
    runtimes: list[tuple[str, str]] = []

    def _replace_cdn(match: re.Match[str]) -> str:
        pkg = match.group(1)
        spec = match.group(2)
        ver = resolve_runtime_version(opener, pkg, spec)
        runtimes.append((pkg, ver))
        rt_id = f"{pkg}@{ver}"
        return f'<script src="./_player/runtime/{rt_id}/rive.js" data-rive-runtime="{rt_id}"></script>'

    patched = PRECONNECT_RE.sub("", html_text)
    patched = CDN_SCRIPT_RE.sub(_replace_cdn, patched)
    patched = ROOT_ATTR_RE.sub(r"\1=\2./\3\2", patched)
    return patched, runtimes


def patch_hosted_js(js_text: str, default_runtime: str) -> str:
    patched = js_text

    # 1. Neutralize /_v analytics beacon in $()
    beacon_re = re.compile(
        r'function \$\(\)\{if\(_\|\|typeof navigator\.sendBeacon!="function"\)return;_=!0;.*?navigator\.sendBeacon\("/_v\?"\+o\.toString\(\)\)\}catch\{\}\}'
    )
    if beacon_re.search(patched):
        patched = beacon_re.sub("function $(){_=!0;}", patched, count=1)
    else:
        patched = re.sub(
            r'try\{navigator\.sendBeacon\([^)]*\)\}catch\{\}',
            "",
            patched,
        )

    # 2. Relative path for drawer.riv
    patched = patched.replace('const Q="/_player/drawer.riv"', 'const Q="./_player/drawer.riv"')
    patched = patched.replace("const Q='/_player/drawer.riv'", "const Q='./_player/drawer.riv'")

    # 3. Configure RuntimeLoader to resolve local WASM assets relative to window.location.href
    loader_anchor = 'function F(){if(typeof rive>"u"){C("This animation could not be loaded.");return}'
    loader_inject = (
        loader_anchor
        + 'if(rive.RuntimeLoader){'
        + 'const rtEl=document.querySelector("script[data-rive-runtime]");'
        + f'const rtPkg=rtEl?rtEl.getAttribute("data-rive-runtime"):"{default_runtime}";'
        + 'const rtBase=new URL("./_player/runtime/"+rtPkg+"/",window.location.href).href;'
        + 'rive.RuntimeLoader.setWasmUrl(rtBase+"rive.wasm");'
        + 'rive.RuntimeLoader.setWasmFallbackUrl(rtBase+"rive_fallback.wasm");'
        + 'if(typeof rive.RuntimeLoader.setWasmThreadsUrl==="function"){'
        + 'rive.RuntimeLoader.setWasmThreadsUrl(rtPkg.indexOf("threads")!==-1?rtBase+"rive_threads.wasm":null);'
        + '}}'
    )
    if loader_anchor not in patched:
        raise ValueError("Unable to locate function F() anchor in hosted.js")
    patched = patched.replace(loader_anchor, loader_inject, 1)

    # 4. Disable dynamic CDN script loader H()
    h_re = re.compile(
        r'function H\(\)\{const e=new URL\(window\.location\.href\)\.searchParams\.get\("runtime"\).*?return window\.crossOriginIsolated===!0&&\(c\|\|!u\)&&s\.push\(i\+"webgl2-threads@"\+\(c\?d:"2\.x\.x"\)\+"/rive\.js"\),c&&s\.push\(i\+"webgl2@"\+d\+"/rive\.js"\),s\}'
    )
    if not h_re.search(patched):
        raise ValueError("Unable to locate function H() in hosted.js")
    patched = h_re.sub("function H(){return[]}", patched, count=1)

    for forbidden in ("cdn.jsdelivr.net", "unpkg.com", "sendBeacon", "/_v", '"/_player/', "'/_player/"):
        if forbidden in patched:
            raise ValueError(f"Patched hosted.js still contains forbidden reference: {forbidden}")
    return patched


def render_server_py(slug: str) -> str:
    return f'''#!/usr/bin/env python3
"""Standalone offline HTTP server for {slug}.

Injects Cross-Origin-Opener-Policy and Cross-Origin-Embedder-Policy headers
required by SharedArrayBuffer (@rive-app/webgl2-threads).
"""

from __future__ import annotations

import argparse
import mimetypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

EXTRA_MIME_TYPES = {{
    ".wasm": "application/wasm",
    ".riv": "application/octet-stream",
    ".webp": "image/webp",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}}


class AppOfflineHandler(SimpleHTTPRequestHandler):
    """Static file handler with COOP/COEP headers and WASM/Rive MIME types."""

    extensions_map = {{**SimpleHTTPRequestHandler.extensions_map, **EXTRA_MIME_TYPES}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def guess_type(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        if ext in EXTRA_MIME_TYPES:
            return EXTRA_MIME_TYPES[ext]
        guessed, _ = mimetypes.guess_type(path)
        return guessed or "application/octet-stream"


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve {slug} offline.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8791, help="Bind port (default: 8791)")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), AppOfflineHandler)
    print(f"Serving {slug} at http://{{args.host}}:{{args.port}}/ (COOP+COEP enabled)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nShutting down server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
'''


def render_start_bat(slug: str) -> str:
    return f"""@echo off
setlocal
cd /d "%~dp0"

set "PY_EXE=C:\\Users\\ManHua\\AppData\\Local\\Programs\\Python\\Python313\\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

echo Starting {slug} offline server on http://127.0.0.1:8791/ ...
start "" http://127.0.0.1:8791/
"%PY_EXE%" server.py --host 127.0.0.1 --port 8791
"""


def render_origin_md(
    host_entry: dict[str, Any],
    app_dir: Path,
    runtimes: list[tuple[str, str]],
    has_embed: bool,
    has_poster: bool,
) -> str:
    slug = host_entry["slug"]
    url = host_entry["url"]
    title = host_entry.get("title") or slug
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    rt_desc = ", ".join(f"`@rive-app/{pkg}@{ver}`" for pkg, ver in runtimes)

    inventory_rows = []
    for root, _, files in os.walk(app_dir):
        for fname in sorted(files):
            fpath = Path(root) / fname
            if fname == "ORIGIN.md" or fname.endswith(".part"):
                continue
            rel = fpath.relative_to(app_dir).as_posix()
            inventory_rows.append((rel, fpath.stat().st_size))
    inventory_rows.sort()
    table_lines = "\n".join(f"| `{rel}` | {size:,} |" for rel, size in inventory_rows)

    embed_line = f"- **Embed URL**: `{url.rstrip('/')}/embed.html`\n" if has_embed else ""
    poster_note = "Captured `poster.webp` (HTTP 200)." if has_poster else "`poster.webp` not present upstream (HTTP 404)."

    return f"""# Origin & Provenance: `{slug}`

- **Original URL**: `{url}`
{embed_line}- **Page Title**: `{title}`
- **Rive Runtime**: {rt_desc}
- **Bundled Date (UTC)**: `{today}`
- **Poster**: {poster_note}

## Offline Modifications

1. **Local Runtime & WASM Assets**:
   - Bundled {rt_desc} (`rive.js`, `rive.wasm`, `rive_fallback.wasm`{', `rive_threads.wasm`' if any(p == 'webgl2-threads' for p, _ in runtimes) else ''}) under `./_player/runtime/`.
   - Removed external `<link rel="preconnect">` tags (`cdn.jsdelivr.net`, `unpkg.com`).
   - Rewrote root-relative paths (`/file.riv`, `/_player/...`) to relative `./` paths for both standalone `server.py` (`/`) and root `rive.site/server.py` (`/{slug}/`).
2. **Patched `_player/hosted.js`**:
   - Neutralized remote `/_v` `navigator.sendBeacon` analytics ping.
   - Pointed `drawer.riv` (`Q`) to `./_player/drawer.riv`.
   - Configured `rive.RuntimeLoader` (`setWasmUrl`, `setWasmFallbackUrl`, `setWasmThreadsUrl`) via `window.location.href`.
   - Disabled dynamic CDN `<script>` injection in `H()`.
3. **COOP / COEP Isolation Server (`server.py`, `start.bat`)**:
   - Sends `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Embedder-Policy: require-corp`, and `Cross-Origin-Resource-Policy: same-origin`.

## Bundled File Inventory

| File | Size (bytes) |
|---|---:|
{table_lines}
"""


def is_already_bundled(app_dir: Path, expected_riv_bytes: int | None) -> bool:
    required = [
        app_dir / "index.html",
        app_dir / "file.riv",
        app_dir / "_player" / "hosted.js",
        app_dir / "_player" / "hosted.css",
        app_dir / "_player" / "drawer.riv",
        app_dir / "_player" / "favicon.ico",
        app_dir / "server.py",
        app_dir / "start.bat",
    ]
    if not all(p.is_file() for p in required):
        return False
    riv_size = (app_dir / "file.riv").stat().st_size
    if expected_riv_bytes is not None and riv_size != expected_riv_bytes:
        return False
    return riv_size > 0


def bundle_host(
    host_entry: dict[str, Any],
    out_dir: Path,
    shared_rt_root: Path,
    proxy_url: str | None,
    force: bool = False,
) -> dict[str, Any]:
    slug = host_entry["slug"]
    base_url = host_entry["url"].rstrip("/")
    app_dir = out_dir / slug
    expected_riv = host_entry.get("file_riv_bytes")

    if not force and is_already_bundled(app_dir, expected_riv):
        return {
            "slug": slug,
            "status": "skipped_existing",
            "file_riv_bytes": (app_dir / "file.riv").stat().st_size,
            "runtimes": [],
        }

    opener = build_opener(proxy_url)
    app_dir.mkdir(parents=True, exist_ok=True)
    player_dir = app_dir / "_player"
    player_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch and patch index.html
    _, _, raw_index = fetch_bytes(opener, f"{base_url}/", timeout=30, retries=3)
    index_html, main_runtimes = patch_html(opener, raw_index.decode("utf-8", errors="replace"))
    if not main_runtimes:
        raise ValueError(f"No @rive-app runtime script found in {base_url}/")
    (app_dir / "index.html").write_text(index_html, encoding="utf-8")

    # 2. Optional embed.html
    all_runtimes: list[tuple[str, str]] = list(main_runtimes)
    st_emb, _, raw_emb = fetch_bytes(opener, f"{base_url}/embed.html", timeout=25, retries=2, allow_404=True)
    has_embed = st_emb == 200 and len(raw_emb) > 0
    if has_embed:
        embed_html, emb_runtimes = patch_html(opener, raw_emb.decode("utf-8", errors="replace"))
        (app_dir / "embed.html").write_text(embed_html, encoding="utf-8")
        for rt in emb_runtimes:
            if rt not in all_runtimes:
                all_runtimes.append(rt)

    # 3. Download/cache shared runtimes & hardlink into app_dir/_player/runtime/
    for pkg, ver in all_runtimes:
        shared_rt_dir = ensure_shared_runtime(opener, shared_rt_root, pkg, ver)
        link_or_copy_runtime(shared_rt_dir, player_dir / "runtime" / f"{pkg}@{ver}")

    # 4. Fetch & patch _player/hosted.js
    _, _, raw_hjs = fetch_bytes(opener, f"{base_url}/_player/hosted.js", timeout=30, retries=3)
    default_rt_id = f"{main_runtimes[0][0]}@{main_runtimes[0][1]}"
    patched_hjs = patch_hosted_js(raw_hjs.decode("utf-8", errors="replace"), default_rt_id)
    (player_dir / "hosted.js").write_text(patched_hjs, encoding="utf-8")

    # 5. Fetch _player static assets
    for rel_path in ("hosted.css", "drawer.riv", "favicon.ico"):
        _, _, raw_asset = fetch_bytes(opener, f"{base_url}/_player/{rel_path}", timeout=30, retries=3)
        (player_dir / rel_path).write_bytes(raw_asset)

    # 6. Optional poster.webp
    st_poster, _, raw_poster = fetch_bytes(
        opener, f"{base_url}/poster.webp", timeout=25, retries=2, allow_404=True
    )
    has_poster = st_poster == 200 and len(raw_poster) > 0
    if has_poster:
        (app_dir / "poster.webp").write_bytes(raw_poster)

    # 7. Stream file.riv (supports 289 MB+ payloads cleanly)
    riv_path = app_dir / "file.riv"
    if not force and riv_path.is_file() and expected_riv and riv_path.stat().st_size == expected_riv:
        riv_bytes = expected_riv
    else:
        riv_bytes = stream_to_file(
            opener,
            f"{base_url}/file.riv",
            riv_path,
            timeout=300,
            retries=3,
            expected_bytes=expected_riv,
        )

    # 8. Write standalone server.py, start.bat, ORIGIN.md
    (app_dir / "server.py").write_text(render_server_py(slug), encoding="utf-8")
    (app_dir / "start.bat").write_text(render_start_bat(slug), encoding="utf-8")
    (app_dir / "ORIGIN.md").write_text(
        render_origin_md(host_entry, app_dir, all_runtimes, has_embed, has_poster),
        encoding="utf-8",
    )

    return {
        "slug": slug,
        "status": "bundled",
        "file_riv_bytes": riv_bytes,
        "runtimes": [f"{p}@{v}" for p, v in all_runtimes],
        "has_embed": has_embed,
        "has_poster": has_poster,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline-bundle alive *.rive.to apps.")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--proxy", default=DEFAULT_PROXY)
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--slugs", nargs="*", help="Optional subset of slugs to bundle")
    args = parser.parse_args()

    proxy_url = None if args.no_proxy else args.proxy
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    targets = [h for h in catalog["hosts"] if h.get("file_riv_status") == 200]
    if args.slugs:
        wanted = set(args.slugs)
        targets = [h for h in targets if h["slug"] in wanted]

    shared_rt_root = args.out_dir / "_shared_runtime"
    shared_rt_root.mkdir(parents=True, exist_ok=True)

    print(f"Targets: {len(targets)} hosts | Out: {args.out_dir} | Proxy: {proxy_url or 'NONE'} | Workers: {args.workers}")
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        fut_map = {
            pool.submit(bundle_host, h, args.out_dir, shared_rt_root, proxy_url, args.force): h["slug"]
            for h in targets
        }
        for idx, fut in enumerate(concurrent.futures.as_completed(fut_map), 1):
            slug = fut_map[fut]
            try:
                res = fut.result()
                results.append(res)
                mb = res["file_riv_bytes"] / (1024 * 1024)
                print(f"[{idx}/{len(targets)}] {res['status']:16s} {slug} ({mb:.2f} MB) {res['runtimes']}", flush=True)
            except Exception as exc:
                failures.append({"slug": slug, "error": str(exc)})
                print(f"[{idx}/{len(targets)}] FAILED           {slug}: {exc}", flush=True)

    print(
        f"\nSummary: bundled={sum(r['status']=='bundled' for r in results)}, "
        f"skipped={sum(r['status']=='skipped_existing' for r in results)}, "
        f"failed={len(failures)}"
    )
    if failures:
        for f in failures:
            print(f"  FAIL {f['slug']}: {f['error']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
