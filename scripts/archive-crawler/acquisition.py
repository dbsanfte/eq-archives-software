"""Serial Wayback acquisition into private staging; exact URL/date manifests."""

import json
from pathlib import Path
import subprocess

from common import CrawlError, Page, TIERS, decode, digest, now, original_url, tier


class Downloader:
    def __init__(self, store, args):
        self.store = store
        self.previous = store.get("wayback_transport", {}) or {}
        remaining = {key: maximum - self.previous.get(key, 0) for key, maximum in
                     (("requests", args.max_requests), ("bytes", args.max_bytes), ("seconds", args.max_seconds))}
        if min(remaining.values()) <= 0:
            raise CrawlError("Cumulative Wayback budget reached; staged evidence retained")
        self.process = subprocess.Popen(["ruby", str(Path(__file__).with_name("downloader.rb"))],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.call({"op": "configure", "delay": args.delay, "bytes_per_second": args.bytes_per_second,
                       "max_requests": remaining["requests"], "max_response_bytes": args.max_page_bytes,
                       "max_total_bytes": remaining["bytes"], "max_seconds": remaining["seconds"]})
        except BaseException:
            self.close()
            raise

    def call(self, job):
        self.process.stdin.write(json.dumps(job) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise CrawlError("Downloader stopped; upstream output omitted")
        try:
            response = json.loads(line)
        except ValueError:
            raise CrawlError("Downloader returned invalid output") from None
        transport = response.get("transport")
        if transport:
            self.store.set("wayback_transport", {key: self.previous.get(key, 0) + value for key, value in transport.items()})
        if not response["ok"]:
            raise CrawlError(response["error"])
        return response["result"]

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()


def sample(args, store):
    downloader = Downloader(store, args)
    store.set("acquisition_started", now())
    successes = attempts = 0
    try:
        candidates = store.candidates()[:args.max_candidates]
        candidates.sort(key=lambda candidate: candidate["state"] == "unavailable")
        for candidate in candidates:
            if json.loads(candidate["captures"]):
                continue
            if candidate["state"] in ("unavailable", "sample_error", "identity_unresolved") and not args.retry_unresolved:
                continue
            attempts += 1
            captures, listings = [], []
            try:
                for level, (start, end) in TIERS.items():
                    listing = downloader.call({"op": "list", "url": candidate["url"], "from": start, "to": end})
                    listings.append({"tier": level, "matched": len(listing["captures"]), "limited": listing["listing_limited"],
                                     "available_rows": listing["available_rows"], "identity_variants": listing["identity_variants"]})
                    if not listing["captures"]:
                        continue
                    records = sorted(listing["captures"], key=lambda r: r["timestamp"])
                    selected = [records[0]]
                    if len(records) > 1 and args.samples_per_candidate > 1:
                        selected.append(records[-1])
                    for record in selected:
                        folder = store.root / "captures" / candidate["id"]
                        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                        destination = folder / (record["timestamp"] + ".html")
                        result = downloader.call({"op": "capture", "url": record["url"], "timestamp": record["timestamp"],
                                                  "from": start, "to": end, "destination": str(destination)})
                        if original_url(result["url"]) != candidate["url"] or tier(result["timestamp"]) != level:
                            raise CrawlError("Returned capture failed exact identity/date validation")
                        data = destination.read_bytes()
                        if digest(data) != result["sha256"]:
                            raise CrawlError("Capture hash mismatch")
                        text, encoding = decode(data, result.get("content_type") or "")
                        page = Page(result["url"])
                        page.feed(text)
                        if not " ".join(page.text).strip():
                            raise CrawlError("Capture contains no readable source text")
                        result.update({"path": str(destination.relative_to(store.root)), "tier": level,
                                       "cdx_digest": record["digest"], "cdx_length": record["length"],
                                       "retrieved_at": now(), "encoding": encoding, "title": " ".join(page.title),
                                       "source": "wayback", "site_coverage": "bounded_samples"})
                        captures.append(result)
                        # Persist each completed artifact before the next request.
                        store.db.execute("UPDATE candidates SET captures=?,state='sampled',error=NULL WHERE id=?",
                                         (json.dumps(captures), candidate["id"]))
                        store.db.commit()
                    break  # Prefer tier 1; use tier 2 when tier 1 has no captures.
                variants = any(item["identity_variants"] for item in listings)
                state = "sampled" if captures else "identity_unresolved" if variants else "unavailable"
                error = (None if captures else "CDX lists different original URLs; exact source identity remains unresolved"
                         if variants else "No exact HTML captures found within the two tiers")
                store.db.execute("UPDATE candidates SET state=?,error=? WHERE id=?",
                                 (state, error, candidate["id"]))
                store.event(candidate["id"], "sample", {"listings": listings, "captures": len(captures)})
                successes += bool(captures)
            except CrawlError as error:
                successes += bool(captures)
                store.db.execute("UPDATE candidates SET state=?,error=? WHERE id=?",
                                 ("sampled" if captures else "sample_error", str(error), candidate["id"]))
                store.db.commit()
                store.event(candidate["id"], "sample_error", {"error": str(error)})
                if "budget" in str(error) or "422 after" in str(error) or "429 after" in str(error):
                    print("Wayback acquisition paused: " + str(error), flush=True)
                    break
            print(f"Checked {attempts}: {successes} newly staged candidates", flush=True)
    finally:
        downloader.close()
    store.set("sampling_result", {"attempted": attempts, "staged": successes})
