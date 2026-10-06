"""Bounded approved-site traversal using one persistent Wayback downloader."""

from collections import deque
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote_plus, urlsplit

from acquisition import Downloader
from common import CrawlError, Page, Store, TIERS, decode, digest, now, original_url, save, tier, within_scope
from discovery import SKIP
from indexer.capture_enrichment import DEFAULT_POLICY

LIMITS = {"sites": 5, "pages_per_site": 20, "files": 100, "bytes": 64 * 1024 * 1024,
          "requests": 500, "seconds": 1800, "page_bytes": 1024 * 1024}


def verified_source(root, capture):
    path = (Path(root) / capture["path"]).resolve()
    if Path(root).resolve() not in path.parents or path.is_symlink():
        raise CrawlError("Source path escaped staging")
    try:
        if path.stat().st_size != capture["bytes"] or capture["bytes"] > LIMITS["page_bytes"]:
            raise CrawlError("Staged source size changed or exceeds the page limit")
        data = path.read_bytes()
    except OSError:
        raise CrawlError("Staged source is unavailable") from None
    if digest(data) != capture["sha256"] or not original_url(capture["url"]) or not tier(capture["timestamp"]):
        raise CrawlError("Staged capture failed source integrity checks")
    return data


def archive_path(capture):
    # Match the public downloader's Linux all-timestamps convention, including
    # CGI::unescape and extensionless-page directory/index.html handling. Keep
    # exact original URL identity in the manifest; refuse ambiguous collisions.
    url = capture["url"]
    canonical = original_url(url)
    if not canonical or not tier(capture["timestamp"]):
        raise CrawlError("Invalid source URL or timestamp for archive destination")
    try:
        path = unquote_plus(url.split("/", 3)[3] if len(url.split("/", 3)) == 4 else "", errors="strict")
    except UnicodeError:
        raise CrawlError("URL has an invalid encoded archive filename") from None
    if (any(ord(c) < 32 or ord(c) == 127 for c in path) or "\\" in path or path.startswith("/")
            or any(piece in (".", "..") or len(piece.encode()) > 255 for piece in path.split("/"))):
        raise CrawlError("URL cannot be safely represented in the existing archive layout")
    if not path or url.endswith("/") or "." not in path.rstrip("/").split("/")[-1]:
        path = path.rstrip("/") + ("/" if path else "") + "index.html"
    return f"websites/{urlsplit(canonical).netloc}/{capture['timestamp']}/{path}"


def document(root, capture):
    raw = verified_source(root, capture)
    text, encoding = decode(raw, capture.get("content_type") or "")
    page = Page(capture["url"])
    page.feed(text)
    return page, encoding


def manifest_hash(manifest):
    return digest(manifest)


def check_manifest(root, manifest):
    if manifest.get("schema") != 1 or not 1 <= len(manifest.get("captures", [])) <= LIMITS["files"]:
        raise CrawlError("Invalid capture batch")
    sites = {site["id"]: site for site in manifest.get("sites", [])}
    if not 1 <= len(sites) <= LIMITS["sites"]:
        raise CrawlError("Invalid approved site set")
    seen, size = set(), 0
    for capture in manifest["captures"]:
        verified_source(root, capture)
        site = sites.get(capture.get("candidate_id"))
        allowed = (original_url(capture["url"]) == original_url(site["url"]) if site and site.get("scope_mode") == "page"
                   else within_scope(capture["url"], site["scope"]) if site else False)
        if not allowed:
            raise CrawlError("Capture is outside the approved site scope")
        if capture.get("archive_path") != archive_path(capture) or capture["archive_path"] in seen:
            raise CrawlError("Invalid or duplicate archive destination")
        seen.add(capture["archive_path"])
        size += capture["bytes"]
    if size > LIMITS["bytes"]:
        raise CrawlError("Batch exceeds the source byte limit")


def capture_sites(root, batch_id, sites, downloader_factory=Downloader):
    directory = Path(root) / "batches" / batch_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    draft_path = directory / "draft.json"
    if draft_path.exists():
        draft = json.loads(draft_path.read_text())
    else:
        draft = {"schema": 1, "batch_id": batch_id, "created_at": now(), "limits": LIMITS,
                 "sites": sites, "captures": [], "visited": [], "notes": [], "indexing": dict(DEFAULT_POLICY)}
        for site in sites:
            for capture in site["captures"]:
                verified_source(root, capture)
                path = archive_path(capture)
                existing = next((item for item in draft["captures"] if item["archive_path"] == path), None)
                if existing:
                    if original_url(existing["url"]) != original_url(capture["url"]) or existing["sha256"] != capture["sha256"]:
                        raise CrawlError("Sample sources collide in the existing archive layout")
                    continue
                draft["captures"].append({**capture, "candidate_id": site["id"], "archive_path": path})
        save(draft_path, draft)
    # Each batch owns a separate durable, cumulative HTTP budget.
    transport_store = Store(directory)
    downloader = None
    seen = {(c["candidate_id"], original_url(c["url"])) for c in draft["captures"]}
    visited = {tuple(entry) for entry in draft["visited"]}
    args = SimpleNamespace(delay=3, bytes_per_second=131072, max_requests=LIMITS["requests"],
                           max_page_bytes=LIMITS["page_bytes"], max_bytes=LIMITS["bytes"], max_seconds=LIMITS["seconds"])
    def acquire(job):
        try:
            return downloader.call(job)
        except CrawlError as error:
            if str(error).startswith(("Wayback HTTP 404", "Wayback HTTP 410", "Response exceeds byte limit", "Expanded response exceeds byte limit")):
                draft["notes"].append({"url": job["url"], "note": str(error) + "; URL excluded"})
                return None
            raise
    try:
        for site in sites:
            frontier = deque([site["scope"], site["url"]])
            for capture in site.get('reviewed_captures', []):
                page, _ = document(root, capture)
                frontier.extend(link['url'] for link in page.links)
            for capture in draft["captures"]:
                if capture["candidate_id"] == site["id"]:
                    page, _ = document(root, capture)
                    frontier.extend(link["url"] for link in page.links)
            checked = set()
            while frontier:
                url = original_url(frontier.popleft())
                allowed = url == original_url(site["url"]) if site.get("scope_mode") == "page" else within_scope(url, site["scope"]) if url else False
                if not url or url in checked or not allowed or SKIP.search(urlsplit(url).path):
                    continue
                checked.add(url)
                if (site["id"], url) in seen or (site["id"], url) in visited:
                    continue
                # Count attempts, including unavailable URLs, to keep traversal finite.
                attempted = sum(key[0] == site["id"] for key in visited)
                if attempted >= LIMITS["pages_per_site"] or len(draft["captures"]) >= LIMITS["files"]:
                    draft["notes"].append({"url": url, "note": "Traversal limit reached; bounded subset staged for review"})
                    break
                if sum(c["bytes"] for c in draft["captures"]) >= LIMITS["bytes"] - LIMITS["page_bytes"]:
                    raise CrawlError("Batch source byte budget reached")
                downloader = downloader or downloader_factory(transport_store, args)
                for level, (start, end) in TIERS.items():
                    listing = acquire({"op": "list", "url": url, "from": start, "to": end})
                    if listing is None:
                        break
                    records = sorted(listing["captures"], key=lambda row: row["timestamp"])
                    if not records:
                        continue
                    record = records[0]
                    folder = directory / "captures" / site["id"]
                    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination = folder / (digest(url) + "-" + record["timestamp"] + ".html")
                    capture = acquire({"op": "capture", "url": record["url"], "timestamp": record["timestamp"],
                                       "from": start, "to": end, "destination": str(destination)})
                    if capture is None:
                        break
                    if original_url(capture["url"]) != url or tier(capture["timestamp"]) != level:
                        raise CrawlError("Downloaded source changed original URL or date tier")
                    capture.update(path=str(destination.relative_to(root)), tier=level, source="wayback",
                                   candidate_id=site["id"], retrieved_at=now(), cdx_digest=record["digest"],
                                   cdx_length=record["length"], site_coverage="bounded_link_traversal")
                    page, encoding = document(root, capture)
                    capture.update(encoding=encoding, title=" ".join(page.title), archive_path=archive_path(capture))
                    collision = next((item for item in draft["captures"] if item["archive_path"] == capture["archive_path"]), None)
                    if collision:
                        if original_url(collision["url"]) != url or collision["sha256"] != capture["sha256"]:
                            draft["notes"].append({"url": url, "note": "Excluded: archive filename collides with another capture"})
                    elif " ".join(page.text).strip():
                        draft["captures"].append(capture)
                        seen.add((site["id"], url))
                        frontier.extend(link["url"] for link in page.links)
                    else:
                        draft["notes"].append({"url": url, "note": "No readable source text"})
                    break
                visited.add((site["id"], url))
                draft["visited"] = sorted(visited)
                save(draft_path, draft)
    except CrawlError as error:
        if "budget" not in str(error).lower():
            raise
        draft["notes"].append({"url": url, "note": str(error) + "; bounded subset staged for review"})
    finally:
        if downloader:
            downloader.close()
        draft["transport"] = transport_store.get("wayback_transport", {})
        save(draft_path, draft)
        transport_store.close()
    # Every newly acquired file needs a second, explicit batch publication approval.
    check_manifest(root, draft)
    return draft
