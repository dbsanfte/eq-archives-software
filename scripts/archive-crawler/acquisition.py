"""Serial Wayback acquisition into private staging; exact URL/date manifests."""

import json
from pathlib import Path
import subprocess
import time

from common import CrawlError, Page, TIERS, decode, digest, in_capture_window, now, original_url, site_scope, tier, within_capture_scope
from wayback_transport import TransportUnavailable, current_transport


class Downloader:
    def __init__(self, store, args):
        self.store = store
        self.previous = store.get("wayback_transport", {}) or {}
        remaining = {key: maximum - self.previous.get(key, 0) for key, maximum in
                     (("requests", args.max_requests), ("bytes", args.max_bytes), ("seconds", args.max_seconds))}
        if min(remaining.values()) <= 0:
            raise CrawlError("Cumulative Wayback budget reached; staged evidence retained")
        self.shared = current_transport.get()
        if self.shared is not None:
            self.args, self.started = args, time.monotonic()
            self.usage = dict(self.previous)
            self.closed = False
            return
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
        if self.shared is not None:
            return self.shared_call(job)
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

    def shared_call(self, job):
        if self.closed:
            raise CrawlError('Downloader session is closed')
        deadline = self.started + self.args.max_seconds - self.previous.get('seconds', 0)
        try:
            with self.shared.turn(deadline):
                remaining = {key: maximum - self.usage.get(key, 0) for key, maximum in
                             (('requests', self.args.max_requests), ('bytes', self.args.max_bytes))}
                seconds = deadline - time.monotonic()
                if min(*remaining.values(), seconds) <= 0:
                    raise CrawlError('Cumulative Wayback budget reached; staged evidence retained')
                try:
                    response = self.shared.request({**job, 'transport': {
                        'delay': self.args.delay, 'bytes_per_second': self.args.bytes_per_second,
                        'max_requests': remaining['requests'], 'max_total_bytes': remaining['bytes'],
                        'max_response_bytes': self.args.max_page_bytes, 'max_seconds': seconds}})
                except CrawlError:
                    # An unreadable/lost response cannot establish actual usage.
                    # Retain the remaining reservation until an explicit extension.
                    self.usage.update(requests=self.args.max_requests, bytes=self.args.max_bytes)
                    raise
                for key in ('requests', 'bytes', 'connections'):
                    self.usage[key] = self.usage.get(key, 0) + response['transport'].get(key, 0)
                if not response['ok']:
                    raise CrawlError(response['error'])
                return response['result']
        finally:
            # Waiting, grading and local work still count against this session's
            # wall-clock allowance. A competing site cannot reset its deadline.
            self.usage['seconds'] = self.previous.get('seconds', 0) + time.monotonic() - self.started
            self.store.set('wayback_transport', self.usage)

    def close(self):
        if self.shared is not None:
            self.closed = True
            return
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()


def redirected_listing(store, candidate, listing, downloader, start, end):
    """Use a verified archived entry point, without changing candidate identity."""
    records = sorted(listing.get('redirects', []), key=lambda row: row['timestamp'])
    selected = records[:1] + (records[-1:] if len(records) > 1 else [])
    mode = json.loads(candidate['coverage'] or '{}').get('scope_mode', 'directory')
    original = original_url(candidate['url'])
    for record in selected:
        if original_url(record['url']) != original or not in_capture_window(record['timestamp'], {'from':start, 'to':end}):
            raise CrawlError('Archived redirect changed original URL or date')
        key = 'entry_redirect:' + digest([record['url'], record['timestamp'], record['digest']])
        resolution = store.get(key)
        if not resolution:
            try:
                resolution = downloader.call({'op':'resolve', 'url':record['url'], 'timestamp':record['timestamp'], 'from':start, 'to':end})
            except CrawlError as error:
                if str(error) in ('Wayback HTTP 404', 'Wayback HTTP 410'):
                    continue
                raise
        target = original_url(resolution.get('url'))
        if (original_url(resolution.get('requested_url')) != original or resolution.get('requested_timestamp') != record['timestamp']
                or not target or not in_capture_window(resolution.get('timestamp'), {'from':start, 'to':end})):
            raise CrawlError('Archived redirect failed exact URL/date validation')
        chain = resolution.get('redirects', [])
        previous = original
        if not 1 <= len(chain) <= 5:
            raise CrawlError('Archived redirect chain could not be verified')
        for hop in chain:
            def replay_url(value):
                return original_url('https://web.archive.org' + value) if isinstance(value, str) and value.startswith('/web/') else None
            source, destination = replay_url(hop.get('from')), replay_url(hop.get('to'))
            if source != previous or not destination or hop.get('status') not in (301, 302, 303, 307, 308):
                raise CrawlError('Archived redirect chain could not be verified')
            allowed = destination == original if mode == 'page' else (within_capture_scope(destination, candidate['scope']) and within_capture_scope(destination, site_scope(original)))
            if not allowed:
                raise CrawlError('Archived redirect is outside the saved capture scope: ' + destination + '. Choose a scope containing the destination or submit it separately.')
            previous = destination
        if previous != target or target == original:
            raise CrawlError('Archived redirect chain does not match its destination')
        store.set(key, resolution)
        result = downloader.call({'op':'list', 'url':resolution['url'], 'from':start, 'to':end})
        if result['captures']:
            return result, resolution
        if result.get('listing_limited'):
            raise CrawlError('Wayback listing limit reached; archived destination availability remains unresolved')
    return listing, None


def sample(args, store, *, candidates=None, downloader=None):
    owned = downloader is None
    downloader = downloader or Downloader(store, args)
    store.set("acquisition_started", now())
    successes = attempts = 0
    try:
        candidates = list(store.candidates()[:args.max_candidates] if candidates is None else candidates)
        candidates.sort(key=lambda candidate: candidate["state"] == "unavailable")
        for candidate in candidates:
            if json.loads(candidate["captures"]):
                continue
            if candidate["state"] in ("unavailable", "sample_error", "identity_unresolved") and not args.retry_unresolved:
                continue
            attempts += 1
            captures, listings = [], []
            mode = json.loads(candidate['coverage'] or '{}').get('scope_mode', 'directory')
            board = mode == 'sitepowerup'
            try:
                for level, (start, end) in TIERS.items():
                    if board:
                        from sitepowerup import sample_listing
                        listing = sample_listing(downloader, candidate['url'], start, end)
                    else:
                        listing = downloader.call({"op": "list", "url": candidate["url"], "from": start, "to": end})
                    listings.append({"tier": level, "matched": len(listing["captures"]), "limited": listing["listing_limited"],
                                     "available_rows": listing["available_rows"], "identity_variants": listing["identity_variants"],
                                     'response_types': listing.get('response_types', [])})
                    entry_redirect = None
                    if not listing['captures'] and mode not in ('ezboard', 'sitepowerup') and listing.get('redirects'):
                        listing, entry_redirect = redirected_listing(store, candidate, listing, downloader, start, end)
                    if not listing["captures"]:
                        if listing.get('listing_limited'):
                            raise CrawlError('Wayback listing limit reached; capture availability remains unresolved')
                        continue
                    records = sorted(listing["captures"], key=lambda r: r["timestamp"])
                    selected = [records[0]]
                    if len(records) > 1 and args.samples_per_candidate > 1:
                        selected.append(records[-1])
                    for record in selected:
                        folder = store.root / "captures" / candidate["id"]
                        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                        suffix = '-' + digest(record['url'])[:16] if board or entry_redirect else ''
                        destination = folder / (record["timestamp"] + suffix + ".html")
                        result = downloader.call({"op": "capture", "url": record["url"], "timestamp": record["timestamp"],
                                                  "from": start, "to": end, "destination": str(destination)})
                        source_url = entry_redirect['url'] if entry_redirect else candidate['url']
                        if original_url(result['url']) != original_url(record['url']) or not in_capture_window(result['timestamp'], {'from':start, 'to':end}) or (not board and original_url(result['url']) != original_url(source_url)):
                            raise CrawlError("Returned capture failed exact identity/date validation")
                        data = destination.read_bytes()
                        if digest(data) != result["sha256"]:
                            raise CrawlError("Capture hash mismatch")
                        text, encoding = decode(data, result.get("content_type") or "")
                        if board:
                            from sitepowerup import board_name, belongs, source_page
                            page, encoding = source_page(data, result['url'], result.get('content_type') or '')
                            if not belongs(page, result['url'], board_name(candidate['url'])):
                                raise CrawlError('SitePowerUp source does not verify the requested board; no grade requested')
                        else:
                            page = Page(result["url"])
                            page.feed(text)
                        if not " ".join(page.text).strip():
                            raise CrawlError("Capture contains no readable source text")
                        result.update({"path": str(destination.relative_to(store.root)), "tier": level,
                                       "cdx_digest": record["digest"], "cdx_length": record["length"],
                                       "retrieved_at": now(), "encoding": encoding, "title": " ".join(page.title),
                                       "source": "wayback", "site_coverage": "bounded_samples"})
                        if entry_redirect:
                            result['entry_redirect'] = entry_redirect
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
                responses = sorted({code for listing in listings for code, _ in listing['response_types']})
                if not captures and not variants and responses:
                    error = 'Wayback has archived responses for this URL, but no usable HTML source was recovered (HTTP ' + ', '.join(responses) + ').'
                store.db.execute("UPDATE candidates SET state=?,error=? WHERE id=?",
                                 (state, error, candidate["id"]))
                store.event(candidate["id"], "sample", {"listings": listings, "captures": len(captures)})
                successes += bool(captures)
            except TransportUnavailable:
                raise
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
        if owned:
            downloader.close()
    store.set("sampling_result", {"attempted": attempts, "staged": successes})
