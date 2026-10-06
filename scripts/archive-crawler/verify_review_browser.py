#!/usr/bin/env python3
"""Real-browser review regressions using disposable, fully local sources."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

from common import Store, digest
from grading import SIGNATURE, sources
from review import decisions, review


def fixture(store):
    data = b'<title>&lt;img src=x onerror="window.injected=true"&gt;</title><p>EverQuest guild history</p><p>&lt;/script&gt;&lt;script&gt;window.injected=true&lt;/script&gt;</p>'
    path = store.root / "capture.html"
    path.write_bytes(data)
    url = "http://guild.example/" + "long-path-" * 20
    capture = {"url": url, "path": path.name, "timestamp": "20000101000000", "tier": 1,
               "bytes": len(data), "sha256": digest(data), "title": '<img src=x onerror="window.injected=true">',
               "timestamp_basis": "memento_datetime"}
    for identifier, captured in (("guild", True), ("unjudged", False)):
        store.db.execute("INSERT INTO candidates(id,url,scope,priority,coverage,evidence,captures,state) VALUES (?,?,?,?,?,?,?,?)",
                         (identifier, url if captured else "http://unjudged.example/", "http://guild.example/", 100,
                          json.dumps({"status": "absent_host"}), "[]", json.dumps([capture] if captured else []),
                          "approval_pending" if captured else "unavailable"))
    store.db.commit()
    documents = sources(store, store.candidates()[0], 120000)
    rating = {"grade": 3, "category": "guild", "confidence": "high", "reason": "Contemporary EQ community.",
              "evidence": [{"slot": 0, "excerpt": "EverQuest guild history"}], "origin": "model",
              "signature": digest({"grader": SIGNATURE, "documents": documents})}
    store.db.execute("UPDATE candidates SET rating=? WHERE id='guild'", (json.dumps(rating),))
    store.db.commit()
    store.set("archive_sha", "a" * 40)
    review(SimpleNamespace(), store)


def main():
    with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
        store = Store(Path(directory) / "state")
        try:
            fixture(store)
            with sync_playwright() as playwright:
                executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
                browser = playwright.chromium.launch(**({"executable_path": executable} if executable else {}))
                try:
                    for width in (320, 390, 1280):
                        context = browser.new_context(viewport={"width": width, "height": 850}, accept_downloads=True)
                        page = context.new_page()
                        errors, requests = [], []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("request", lambda request: requests.append(request.url))
                        page.goto((store.root / "review.html").as_uri())
                        assert page.locator(".card").count() == 1
                        assert page.locator(".card a").first.inner_text().startswith("<img")
                        assert not page.evaluate("Boolean(window.injected)")
                        page.get_by_text("Read captured source — 20000101000000", exact=True).click()
                        assert "EverQuest guild history" in page.locator("pre").inner_text()
                        page.locator(".card select").select_option("approve")
                        page.reload()
                        assert page.locator(".card select").input_value() == "approve"
                        with page.expect_download() as download:
                            page.get_by_role("button", name="Download review decisions").click()
                        destination = Path(directory) / f"decisions-{width}.json"
                        download.value.save_as(destination)
                        exported = json.loads(destination.read_text())
                        assert len(exported) == 1 and exported[0]["decision"] == "approve"
                        assert len(exported[0]["manifest_sha256"]) == 64
                        decisions(SimpleNamespace(file=str(destination)), store)
                        page.get_by_label("Filter candidates").select_option("all")
                        assert page.locator(".card").count() == 2
                        assert page.locator('[data-id="unjudged"] option[value="approve"]').evaluate('(node) => node.disabled')
                        page.get_by_label("Filter candidates").select_option("unjudged")
                        assert page.locator(".card").count() == 1
                        assert not errors, errors
                        assert all(url.startswith("file:") or url.startswith("blob:") for url in requests), requests
                        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                        context.close()
                finally:
                    browser.close()
        finally:
            store.close()
    print("Review browser regressions passed at 320, 390 and 1280 px")


if __name__ == "__main__":
    main()
