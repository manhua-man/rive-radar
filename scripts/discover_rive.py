#!/usr/bin/env python3
"""Initial ecosystem discovery & liveness radar for *.rive.to hosted Rive apps.

Queries strictly public APIs/endpoints via HTTP(S) proxy, deduplicates
discovered *.rive.to hostnames, probes HTTP liveness, <title>, and /file.riv
Content-Length, and outputs:
  - data/rive_catalog.json
  - reports/RIVE_DISCOVERY_REPORT.md
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

RADAR_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = RADAR_ROOT / "data"
REPORTS_DIR = RADAR_ROOT / "reports"

DEFAULT_PROXY = "http://127.0.0.1:17890"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

RIVE_TO_HOST_RE = re.compile(r"\b([a-z0-9][a-z0-9-]{0,62}\.rive\.to)\b", re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
RUNTIME_RE = re.compile(r'data-runtime=["\']([^"\']+)["\']', re.IGNORECASE)
DATA_SRC_RE = re.compile(r'data-src=["\']([^"\']+)["\']', re.IGNORECASE)

SEED_HOSTS = ["game-airplane-yy3501.rive.to"]


def build_opener(proxy_url: str | None) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener


def fetch_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    timeout: int = 25,
    retries: int = 3,
) -> tuple[int, dict[str, str], str]:
    last_exc: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html,*/*"},
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, headers, raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    assert last_exc is not None
    raise last_exc


def extract_hosts_from_text(text: str) -> set[str]:
    found: set[str] = set()
    for m in RIVE_TO_HOST_RE.finditer(text):
        host = m.group(1).lower().strip(".")
        if host != "rive.to" and not host.startswith("www."):
            found.add(host)
    return found


def query_urlscan(opener: urllib.request.OpenerDirector) -> dict[str, Any]:
    url = "https://urlscan.io/api/v1/search/?q=domain:rive.to&size=100"
    hosts: set[str] = set()
    try:
        status, _, body = fetch_text(opener, url, timeout=25)
        data = json.loads(body)
        results = data.get("results", [])
        for item in results:
            page = item.get("page") or {}
            task = item.get("task") or {}
            for candidate in (page.get("domain"), page.get("url"), task.get("domain"), task.get("url")):
                if isinstance(candidate, str):
                    hosts |= extract_hosts_from_text(candidate)
        hosts |= extract_hosts_from_text(body)
        return {"source": "urlscan.io", "url": url, "status": status, "result_count": len(results), "hosts": sorted(hosts), "error": None}
    except Exception as exc:
        return {"source": "urlscan.io", "url": url, "status": None, "result_count": 0, "hosts": [], "error": str(exc)}


def query_wayback(opener: urllib.request.OpenerDirector) -> dict[str, Any]:
    url = "https://web.archive.org/cdx/search/cdx?url=*.rive.to/*&output=json&collapse=urlkey"
    hosts: set[str] = set()
    try:
        status, _, body = fetch_text(opener, url, timeout=35, retries=3)
        rows = json.loads(body) if body.strip() else []
        for row in rows[1:] if len(rows) > 1 else []:
            for cell in row:
                if isinstance(cell, str):
                    hosts |= extract_hosts_from_text(cell)
        return {"source": "wayback_cdx", "url": url, "status": status, "result_count": max(0, len(rows) - 1), "hosts": sorted(hosts), "error": None}
    except Exception as exc:
        return {"source": "wayback_cdx", "url": url, "status": None, "result_count": 0, "hosts": [], "error": str(exc)}


def query_rive_to_root(opener: urllib.request.OpenerDirector) -> dict[str, Any]:
    url = "https://rive.to/"
    try:
        status, _, body = fetch_text(opener, url, timeout=25, retries=2)
        hosts = extract_hosts_from_text(body)
        return {"source": "rive_to_root", "url": url, "status": status, "result_count": len(hosts), "hosts": sorted(hosts), "error": None}
    except Exception as exc:
        return {"source": "rive_to_root", "url": url, "status": None, "result_count": 0, "hosts": [], "error": str(exc)}


def query_hn_algolia(opener: urllib.request.OpenerDirector) -> dict[str, Any]:
    url = "https://hn.algolia.com/api/v1/search?query=rive.to&hitsPerPage=100"
    hosts: set[str] = set()
    try:
        status, _, body = fetch_text(opener, url, timeout=20)
        data = json.loads(body)
        hits = data.get("hits", [])
        hosts |= extract_hosts_from_text(body)
        return {"source": "hn_algolia", "url": url, "status": status, "result_count": len(hits), "hosts": sorted(hosts), "error": None}
    except Exception as exc:
        return {"source": "hn_algolia", "url": url, "status": None, "result_count": 0, "hosts": [], "error": str(exc)}


def query_framer_index(opener: urllib.request.OpenerDirector) -> dict[str, Any]:
    url = "https://framerusercontent.com/sites/3Hh1SxuP94sycwmlcNuNRW/searchIndex-eHmXPTkl7rWL.json"
    hosts: set[str] = set()
    try:
        status, _, body = fetch_text(opener, url, timeout=25)
        hosts |= extract_hosts_from_text(body)
        data = json.loads(body)
        entry_count = len(data) if isinstance(data, (dict, list)) else 0
        return {"source": "rive_framer_search_index", "url": url, "status": status, "result_count": entry_count, "hosts": sorted(hosts), "error": None}
    except Exception as exc:
        return {"source": "rive_framer_search_index", "url": url, "status": None, "result_count": 0, "hosts": [], "error": str(exc)}


def query_rive_public_pages(opener: urllib.request.OpenerDirector) -> list[dict[str, Any]]:
    urls = [
        ("rive_showcase", "https://rive.app/showcase"),
        ("rive_community", "https://rive.app/community"),
        ("rive_marketplace", "https://rive.app/marketplace"),
    ]
    out: list[dict[str, Any]] = []
    for name, url in urls:
        try:
            status, _, body = fetch_text(opener, url, timeout=25)
            hosts = extract_hosts_from_text(body)
            out.append({"source": name, "url": url, "status": status, "result_count": len(hosts), "hosts": sorted(hosts), "error": None})
        except urllib.error.HTTPError as exc:
            out.append({"source": name, "url": url, "status": exc.code, "result_count": 0, "hosts": [], "error": f"HTTP {exc.code}"})
        except Exception as exc:
            out.append({"source": name, "url": url, "status": None, "result_count": 0, "hosts": [], "error": str(exc)})
    return out


def query_github_repos(opener: urllib.request.OpenerDirector) -> dict[str, Any]:
    url = "https://api.github.com/search/repositories?q=rive.to&per_page=50"
    hosts: set[str] = set()
    try:
        status, _, body = fetch_text(opener, url, timeout=20)
        data = json.loads(body)
        items = data.get("items", [])
        hosts |= extract_hosts_from_text(body)
        return {"source": "github_repositories", "url": url, "status": status, "result_count": len(items), "hosts": sorted(hosts), "error": None}
    except Exception as exc:
        return {"source": "github_repositories", "url": url, "status": None, "result_count": 0, "hosts": [], "error": str(exc)}


def query_crtsh(opener: urllib.request.OpenerDirector) -> dict[str, Any]:
    url = "https://crt.sh/?q=%25.rive.to&output=json"
    hosts: set[str] = set()
    try:
        status, _, body = fetch_text(opener, url, timeout=25)
        data = json.loads(body)
        for row in data if isinstance(data, list) else []:
            for field in (row.get("common_name"), row.get("name_value")):
                if isinstance(field, str):
                    hosts |= extract_hosts_from_text(field)
        return {"source": "crt_sh", "url": url, "status": status, "result_count": len(data) if isinstance(data, list) else 0, "hosts": sorted(hosts), "error": None}
    except Exception as exc:
        return {"source": "crt_sh", "url": url, "status": None, "result_count": 0, "hosts": [], "error": str(exc)}


def probe_host(opener: urllib.request.OpenerDirector, host: str, sources: list[str]) -> dict[str, Any]:
    base_url = f"https://{host}/"
    record: dict[str, Any] = {
        "host": host,
        "slug": host.removesuffix(".rive.to"),
        "url": base_url,
        "sources": sorted(sources),
        "alive": False,
        "http_status": None,
        "title": None,
        "runtime_version": None,
        "data_src": None,
        "file_riv_status": None,
        "file_riv_bytes": None,
        "file_riv_etag": None,
        "poster_webp_status": None,
        "poster_webp_bytes": None,
        "coop": None,
        "coep": None,
        "error": None,
    }

    try:
        status, headers, body = fetch_text(opener, base_url, timeout=15)
        record["http_status"] = status
        record["coop"] = headers.get("cross-origin-opener-policy")
        record["coep"] = headers.get("cross-origin-embedder-policy")
        title_m = TITLE_RE.search(body)
        if title_m:
            record["title"] = html.unescape(re.sub(r"\s+", " ", title_m.group(1)).strip())
        rt_m = RUNTIME_RE.search(body)
        if rt_m:
            record["runtime_version"] = rt_m.group(1).strip()
        src_m = DATA_SRC_RE.search(body)
        if src_m:
            record["data_src"] = src_m.group(1).strip()
        record["alive"] = 200 <= status < 400
    except urllib.error.HTTPError as exc:
        record["http_status"] = exc.code
        record["error"] = f"HTTP {exc.code}"
    except Exception as exc:
        record["error"] = str(exc)

    # Probe /file.riv (or data-src if different)
    riv_path = record["data_src"] or "/file.riv"
    if not riv_path.startswith("/"):
        riv_path = "/" + riv_path
    riv_url = f"https://{host}{riv_path}"
    try:
        req = urllib.request.Request(riv_url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with opener.open(req, timeout=15) as resp:
            record["file_riv_status"] = resp.status
            cl = resp.headers.get("Content-Length")
            record["file_riv_bytes"] = int(cl) if cl and cl.isdigit() else None
            record["file_riv_etag"] = resp.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        record["file_riv_status"] = exc.code
    except Exception:
        pass

    # Probe /poster.webp
    poster_url = f"https://{host}/poster.webp"
    try:
        req = urllib.request.Request(poster_url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with opener.open(req, timeout=12) as resp:
            record["poster_webp_status"] = resp.status
            cl = resp.headers.get("Content-Length")
            record["poster_webp_bytes"] = int(cl) if cl and cl.isdigit() else None
    except urllib.error.HTTPError as exc:
        record["poster_webp_status"] = exc.code
    except Exception:
        pass

    return record


def format_bytes(num: int | None) -> str:
    if num is None:
        return "-"
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num / (1024 * 1024):.2f} MB"


def build_report(catalog: dict[str, Any]) -> str:
    lines: list[str] = [
        "# `*.rive.to` Ecosystem Discovery Report",
        "",
        f"- **Generated (UTC)**: `{catalog['generated_at_utc']}`",
        f"- **Proxy**: `{catalog['proxy']}`",
        f"- **Total Unique Hosts Discovered**: `{catalog['summary']['total_hosts']}`",
        f"- **Alive Hosts (HTTP 2xx/3xx)**: `{catalog['summary']['alive_hosts']}`",
        f"- **Hosts with `/file.riv` (HTTP 200)**: `{catalog['summary']['hosts_with_file_riv']}`",
        *(
            [f"- **Bundled Offline Apps**: `{catalog['summary']['bundled_apps']}`"]
            if "bundled_apps" in catalog["summary"]
            else []
        ),
        "",
        "## 1. Public Source Query Results",
        "",
        "| Source | HTTP Status | Raw Matches | Unique `*.rive.to` Hosts | Notes |",
        "|---|---:|---:|---:|---|",
    ]
    for src in catalog["sources"]:
        err = src["error"] or "ok"
        lines.append(
            f"| `{src['source']}` | {src['status'] or '-'} | {src['result_count']} | {len(src['hosts'])} | {err} |"
        )

    lines.extend([
        "",
        "## 2. Discovered `*.rive.to` Hosts",
        "",
        "| Host | Alive | HTTP | Title | Runtime | `/file.riv` Size | `/poster.webp` | Sources |",
        "|---|:---:|---:|---|---|---:|---:|---|",
    ])
    for item in catalog["hosts"]:
        alive_str = "YES" if item["alive"] else "NO"
        title_str = (item["title"] or "-").replace("|", "\\|")
        rt_str = item["runtime_version"] or "-"
        riv_str = format_bytes(item["file_riv_bytes"]) if item["file_riv_status"] == 200 else f"HTTP {item['file_riv_status'] or '-'}"
        poster_str = format_bytes(item["poster_webp_bytes"]) if item["poster_webp_status"] == 200 else f"HTTP {item['poster_webp_status'] or '-'}"
        srcs = ", ".join(item["sources"])
        lines.append(
            f"| `{item['host']}` | {alive_str} | {item['http_status'] or '-'} | {title_str} | `{rt_str}` | {riv_str} | {poster_str} | {srcs} |"
        )

    alive_candidates = [h for h in catalog["hosts"] if h["alive"] and h["host"] not in SEED_HOSTS]
    lines.extend([
        "",
        "## 3. Batch 1 Capture Candidates (Excluding Seed)",
        "",
    ])
    if alive_candidates:
        for c in alive_candidates:
            lines.append(
                f"- `https://{c['host']}/` — **{c['title'] or c['slug']}** "
                f"(runtime `{c['runtime_version'] or '?'}`, `/file.riv`: {format_bytes(c['file_riv_bytes'])})"
            )
    else:
        lines.append("- No additional live hosts beyond the seed (`game-airplane-yy3501.rive.to`) were returned from the queried public endpoints.")

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover and probe *.rive.to hosted Rive apps.")
    parser.add_argument("--proxy", default=DEFAULT_PROXY, help=f"HTTP/HTTPS proxy URL (default: {DEFAULT_PROXY})")
    parser.add_argument("--no-proxy", action="store_true", help="Disable proxy")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent probe workers (default: 8)")
    args = parser.parse_args()

    proxy = None if args.no_proxy else args.proxy
    opener = build_opener(proxy)

    source_results: list[dict[str, Any]] = []
    source_results.append(query_rive_to_root(opener))
    source_results.append(query_urlscan(opener))
    source_results.append(query_wayback(opener))
    source_results.append(query_hn_algolia(opener))
    source_results.append(query_framer_index(opener))
    source_results.extend(query_rive_public_pages(opener))
    source_results.append(query_github_repos(opener))
    source_results.append(query_crtsh(opener))

    catalog_path = DATA_DIR / "rive_catalog.json"
    report_path = REPORTS_DIR / "RIVE_DISCOVERY_REPORT.md"

    existing_catalog: dict[str, Any] = {}
    if catalog_path.is_file():
        try:
            existing_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except Exception:
            existing_catalog = {}
    existing_by_host: dict[str, dict[str, Any]] = {
        h["host"]: h
        for h in existing_catalog.get("hosts", [])
        if isinstance(h, dict) and h.get("host")
    }
    existing_summary: dict[str, Any] = existing_catalog.get("summary", {})

    host_sources: dict[str, set[str]] = {h: {"seed"} for h in SEED_HOSTS}
    for host, prev in existing_by_host.items():
        host_sources.setdefault(host, set()).update(prev.get("sources") or [])
    for sr in source_results:
        for h in sr["hosts"]:
            host_sources.setdefault(h, set()).add(sr["source"])

    probed: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(probe_host, opener, host, sorted(srcs)): host
            for host, srcs in sorted(host_sources.items())
        }
        for fut in concurrent.futures.as_completed(futures):
            rec = fut.result()
            prev = existing_by_host.get(rec["host"])
            if prev:
                if rec.get("runtime_version") is None and prev.get("runtime_version"):
                    rec["runtime_version"] = prev["runtime_version"]
                for key in ("bundled", "bundle_dir", "has_embed", "has_poster"):
                    if key in prev:
                        rec[key] = prev[key]
            probed.append(rec)

    probed.sort(key=lambda x: (not x["alive"], -(x["file_riv_bytes"] or 0), x["host"]))

    summary: dict[str, Any] = {
        "total_hosts": len(probed),
        "alive_hosts": sum(1 for h in probed if h["alive"]),
        "hosts_with_file_riv": sum(1 for h in probed if h["file_riv_status"] == 200),
    }
    for key in ("bundled_apps", "bundled_at_utc", "unique_disk_bytes", "logical_disk_bytes"):
        if key in existing_summary:
            summary[key] = existing_summary[key]

    catalog = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxy": proxy or "direct",
        "summary": summary,
        "sources": source_results,
        "hosts": probed,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    catalog_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report_path.write_text(build_report(catalog), encoding="utf-8")

    new_hosts = sorted(set(host_sources) - set(existing_by_host))
    print(f"Discovered {catalog['summary']['total_hosts']} unique *.rive.to hosts "
          f"({catalog['summary']['alive_hosts']} alive, {catalog['summary']['hosts_with_file_riv']} with /file.riv; "
          f"{len(new_hosts)} new since previous catalog).")
    if new_hosts:
        print("New hosts:", ", ".join(new_hosts))
    print(f"Wrote {catalog_path}")
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
