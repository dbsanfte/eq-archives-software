"""Add bounded links from already graded staging sources to the cached graph."""

import json
from urllib.parse import urlsplit

from common import CrawlError, Page, decode, site_scope
from captures import verified_source
from discovery import SKIP
from review import checked_sources, record
from state import connect


def staged_links(root, run, *, max_reads=66, max_bytes=16 * 1024 * 1024):
    reads = size = 0
    with connect(root) as main:
        for row in main.candidates():
            item = record(main, row)
            if (item["rating"] or {}).get("grade", -1) < 2:
                continue
            if reads >= max_reads or size >= max_bytes:
                break
            fresh = [capture for capture in item["captures"]
                     if not run.db.execute("SELECT 1 FROM scans WHERE blob=? AND source=?",
                          (capture["sha256"], "staging/" + capture["path"])).fetchone()]
            if not fresh:
                continue
            if sum(capture["bytes"] for capture in item["captures"]) + size > max_bytes:
                continue
            try:
                checked_sources(main, item)
            except CrawlError:
                continue
            for capture in fresh:
                if reads >= max_reads:
                    break
                raw = verified_source(main.root, capture)
                reads += 1
                size += len(raw)
                html, _ = decode(raw, capture.get("content_type") or "")
                page = Page(capture["url"])
                page.feed(html)
                from ezboard import remember_page
                remember_page(run, page, capture)
                source = "staging/" + capture["path"]
                for link in page.links:
                    if (SKIP.search(urlsplit(link["url"]).path) or site_scope(link["url"]) == site_scope(capture["url"])
                            or urlsplit(link["url"]).hostname == "web.archive.org"):
                        continue
                    evidence = {**link, "source": source, "source_url": capture["url"],
                                "source_category": item["rating"]["category"], "source_timestamp": capture["timestamp"],
                                "blob": capture["sha256"], "source_staging_manifest": item["manifest_sha256"],
                                "source_judgment": "model", "source_grade": item["rating"]["grade"]}
                    run.db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?)", (link["url"], source, json.dumps(evidence)))
                run.db.execute("INSERT OR IGNORE INTO scans VALUES (?,?,?)", (capture["sha256"], source, json.dumps({"links": len(page.links)})))
                run.db.commit()
    return {"staged_source_reads": reads, "staged_source_bytes": size}
