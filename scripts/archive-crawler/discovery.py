"""Bounded Git-object reads and a cached link graph; never clone, pull or walk."""

import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

from common import CrawlError, Page, candidate_exclusion, decode, digest, original_url, site_identity, site_scope, tier

SKIP = re.compile(r"\.(?:gif|jpe?g|png|webp|css|js|ico|zip|exe|mp[34]|wav|pdf)(?:$|\?)", re.I)
SIGNALS = re.compile(r"everquest|\beq\b|norrath|guild|class|cleric|druid|shaman|monk|wizard|enchanter|necromancer|bard|paladin|ranger|rogue|warrior|shadow.?knight|news|forum|raid|tradeskill|spell|quest|blog", re.I)


class Archive:
    def __init__(self, repository, store, max_entries=15000, max_inventory=200000):
        self.repo, self.store, self.max_entries = Path(repository).resolve(), store, max_entries
        self.max_inventory = max_inventory
        if self.repo == store.root or self.repo in store.root.parents:
            raise CrawlError("Staging must be outside the archive checkout")
        self.sha = subprocess.check_output(["git", "-c", f"safe.directory={self.repo}", "-C", str(self.repo), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
        common = subprocess.check_output(["git", "-c", f"safe.directory={self.repo}", "-C", str(self.repo), "rev-parse", "--git-common-dir"], stderr=subprocess.DEVNULL).decode().strip()
        self.objects = (self.repo / common / "objects").resolve()
        self.reader = store.root / "git-reader"
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
                    "GIT_OBJECT_DIRECTORY": str(self.objects), "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_SYSTEM": os.devnull}
        if not self.reader.exists():
            subprocess.run(["git", "init", "--bare", "--template=", "-q", str(self.reader)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # A separate, remote-free reader matters on old Git: a later `-c
        # remote.origin.promisor=false` cannot undo earlier promisor callbacks.
        # Read the archive's object database, without loading its Git config.
        if self.git("remote").strip():
            raise CrawlError("Git reader must have no remotes; refusing implicit fetches")
        previous = store.get("archive_sha")
        if previous != self.sha:
            root = self.git("ls-tree", "-z", self.sha + ":websites")
            store.db.execute("DELETE FROM hosts")
            for row in root.split(b"\0"):
                if row:
                    metadata, name = row.split(b"\t", 1)
                    mode, kind, oid = metadata.split()
                    if kind == b"tree":
                        store.db.execute("INSERT INTO hosts VALUES (?,?)", (name.decode("utf-8"), oid.decode()))
            store.set("archive_sha", self.sha)
        if store.get("archive_repository") != str(self.repo):
            store.set("archive_repository", str(self.repo))

    def git(self, *args):
        result = subprocess.run(["git", "--git-dir", str(self.reader), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        if result.returncode:
            raise CrawlError("Archive object unavailable locally; no automatic fetch attempted")
        return result.stdout

    def files(self, host):
        row = self.store.db.execute("SELECT tree FROM hosts WHERE host=?", (host,)).fetchone()
        if not row:
            return [], True
        tree = row[0]
        state = self.store.db.execute("SELECT * FROM tree_state WHERE tree=?", (tree,)).fetchone()
        if not state:
            consumed = self.store.db.execute("SELECT COALESCE(SUM(entries),0) FROM tree_state").fetchone()[0]
            remaining = min(self.max_entries, max(self.max_inventory - consumed, 0))
            if not remaining:
                return [], False
            command = ["git", "--git-dir", str(self.reader), "ls-tree", "-rz", tree]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env)
            count, buffer, complete = 0, b"", True
            try:
                while count < remaining:
                    block = process.stdout.read(4096)
                    if not block:
                        break
                    buffer += block
                    while b"\0" in buffer and count < remaining:
                        record, buffer = buffer.split(b"\0", 1)
                        metadata, name = record.split(b"\t", 1)
                        mode, kind, oid = metadata.split()
                        count += 1
                        if kind == b"blob" and mode in (b"100644", b"100755"):
                            path = name.decode("utf-8", "surrogateescape")
                            if not any(0xD800 <= ord(c) <= 0xDFFF for c in path):
                                self.store.db.execute("INSERT OR IGNORE INTO files VALUES (?,?,?)", (tree, path, oid.decode()))
                if count >= remaining:
                    complete = False
                    process.terminate()
                process.wait(timeout=10)
                if complete and process.returncode:
                    raise CrawlError("Could not enumerate local archive tree")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
            self.store.db.execute("INSERT INTO tree_state VALUES (?,?,?)", (tree, complete, count))
            self.store.db.commit()
        rows = list(self.store.db.execute("SELECT path,blob FROM files WHERE tree=?", (tree,)))
        complete = bool(self.store.db.execute("SELECT complete FROM tree_state WHERE tree=?", (tree,)).fetchone()[0])
        return rows, complete

    def coverage(self, url):
        parsed = urlsplit(url)
        host = parsed.netloc
        exists = self.store.db.execute("SELECT 1 FROM hosts WHERE host=?", (host,)).fetchone()
        if not exists:
            # Old downloader paths occasionally encode a port with an underscore.
            aliases = (host.replace(":", "_"), host + "_443")
            if any(self.store.db.execute("SELECT 1 FROM hosts WHERE host=?", (h,)).fetchone() for h in aliases):
                return {"status": "uncertain_legacy_host", "archive_sha": self.sha}
            return {"status": "absent_host", "archive_sha": self.sha}
        rows, complete = self.files(host)
        path = parsed.path.lstrip("/") + ("?" + parsed.query if parsed.query else "")
        matches = []
        for row in rows:
            timestamp, separator, stored = row["path"].partition("/")
            if separator and tier(timestamp) and (stored == path or stored == path.rstrip("/") + "/index.html" or not path and stored == "index.html"):
                matches.append(timestamp)
        # Legacy filenames lose protocol/URL-escaping information. Keep that
        # uncertainty visible instead of asserting exact URL equivalence.
        ambiguous = parsed.scheme == "https" or "%" in url or "+" in url
        status = ("uncertain_legacy_url" if ambiguous else "present_tier1" if any(tier(t) == 1 for t in matches)
                  else "missing_tier1" if matches else "absent_page" if complete else "inventory_partial")
        return {"status": status, "timestamps": sorted(matches), "complete": complete,
                "identity_basis": "legacy_path", "archive_sha": self.sha}

    def blob(self, oid, limit):
        size = int(self.git("cat-file", "-s", oid))
        if size > limit:
            raise CrawlError("Seed capture exceeds the source byte limit")
        return self.git("cat-file", "blob", oid)


def discover(args, store, *, cached_only=False, deadline=None):
    archive = Archive(args.archive_repo, store, args.max_tree_entries, args.max_inventory_entries)
    seeds = json.loads(Path(args.seeds).read_text())
    work = []
    for seed in ([] if cached_only else seeds):
        if deadline is not None and time.time() >= deadline:
            break
        rows, complete = archive.files(seed["host"])
        for row in rows:
            timestamp, _, path = row["path"].partition("/")
            level = tier(timestamp)
            if not level or SKIP.search(path):
                continue
            rank = 0 if re.search(r"links?|guild|class|forum", path, re.I) else 1 if "index" in path.lower() else 2
            work.append((level, rank, seed["host"], timestamp, path, row["blob"], seed["category"]))
    # Interleave each host's ranked choices within a date tier. Missing blobs
    # consume bounded probes, and never consume that host's successful-read cap.
    work.sort()
    selected, used, positions = [], set(), {}
    for item in work:
        level, rank, host, timestamp, path, blob, category = item
        if blob in used:
            continue
        used.add(blob)
        key = (level, host)
        positions[key] = positions.get(key, 0) + 1
        selected.append((positions[key], item))
    selected.sort(key=lambda entry: (entry[1][0], entry[0], entry[1][2]))
    reads, read_bytes, failures, probes, by_host = 0, 0, 0, 0, {}
    for _, (level, rank, host, timestamp, path, blob, category) in selected:
        if deadline is not None and time.time() >= deadline:
            break
        if by_host.get(host, 0) >= args.max_per_seed:
            continue
        source = f"websites/{host}/{timestamp}/{path}"
        cached = store.db.execute("SELECT evidence FROM scans WHERE blob=? AND source=?", (blob, source)).fetchone()
        if cached:
            by_host[host] = by_host.get(host, 0) + 1
            continue
        if read_bytes >= args.max_seed_bytes or reads >= args.max_seed_captures or probes >= args.max_seed_probes:
            break
        probes += 1
        try:
            data = archive.blob(blob, min(args.max_page_bytes, args.max_seed_bytes - read_bytes))
        except CrawlError:
            failures += 1
            continue
        reads += 1
        by_host[host] = by_host.get(host, 0) + 1
        read_bytes += len(data)
        if b"<" not in data[:4096]:
            store.db.execute("INSERT INTO scans VALUES (?,?,?)", (blob, source, json.dumps({"links": 0})))
            store.db.commit()
            continue
        url = original_url("http://" + host + "/" + path)
        if not url:
            continue
        text, encoding = decode(data)
        page = Page(url)
        page.feed(text)
        from ezboard import remember_page
        remember_page(store, page, {'url': url, 'timestamp': timestamp, 'sha256': digest(data)})
        for link in page.links:
            target = link["url"]
            if SKIP.search(urlsplit(target).path) or urlsplit(target).hostname == "web.archive.org":
                continue
            if urlsplit(target).netloc == host:
                continue  # First pilot discovers outward links, not local navigation.
            evidence = {**link, "source": source, "source_url": url,
                        "source_category": category, "source_timestamp": timestamp, "blob": blob,
                        "source_archive_sha": archive.sha}
            store.db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?)", (target, source, json.dumps(evidence)))
        store.db.execute("INSERT INTO scans VALUES (?,?,?)", (blob, source, json.dumps({"links": len(page.links), "encoding": encoding})))
        store.db.commit()
    rows = list(store.db.execute("SELECT url,evidence FROM links ORDER BY url"))
    grouped = {}
    for row in rows:
        evidence = json.loads(row["evidence"])
        entry = grouped.setdefault(row["url"], [])
        if len(entry) < 8:
            entry.append(evidence)
    ranked = []
    for url, evidence in grouped.items():
        signals = " ".join(e["anchor"] + " " + e["context"] for e in evidence)
        priority = len(SIGNALS.findall(signals)) * 5 + len(SIGNALS.findall(url)) * 3
        priority += len({urlsplit(e["source_url"]).netloc for e in evidence}) * 8
        priority += 10 if any(tier(e["source_timestamp"]) == 1 for e in evidence) else 0
        ranked.append((priority, url, evidence))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    # Until sampling starts, refresh the shortlist as more local evidence is
    # found. Afterwards freeze it so retries cannot silently widen the pilot.
    if not store.get("acquisition_started"):
        store.db.execute("DELETE FROM candidates WHERE state='discovered' AND captures='[]' AND rating IS NULL")
    existing = store.candidates()
    scopes = {site_identity(row["url"], store) for row in existing}
    scopes.update(site_identity(url, store) for url in store.get("excluded_scopes", []))
    added = len(existing)
    unresolved = []
    coverage_pending = []
    for priority, url, evidence in ranked:
        if added >= args.max_candidates or deadline is not None and time.time() >= deadline:
            break
        if candidate_exclusion(url):
            continue
        from ezboard import address, board_url, candidate_url
        linked_url = url
        from sitepowerup import candidate_url as board_candidate, board_url as sitepowerup_board
        url = board_candidate(url)
        if not url:
            continue
        url = candidate_url(store, url)
        if url is None:
            resolution = store.get('ezboard_resolution:' + digest(linked_url)) or {}
            if address(linked_url) and not resolution.get('unresolved') and len(unresolved) < 50:
                unresolved.append(linked_url)
            continue
        if url != linked_url:
            evidence = [{**entry, 'linked_url': linked_url, 'resolved_board_url': url} for entry in evidence]
        scope = site_scope(url)
        identity = site_identity(url)
        if identity in scopes:
            continue
        from site_inventory import SiteInventory
        if not hasattr(archive, "site_inventory"):
            archive.site_inventory = SiteInventory(archive)
        site_coverage = archive.site_inventory.check(url)
        if site_coverage["status"] != "new_site":
            if (board_url(url) or sitepowerup_board(url)) and site_coverage['status'] == 'inventory_partial':
                coverage_pending.append({'url': url, 'result': site_coverage})
            continue
        coverage = archive.coverage(url)
        if coverage["status"] == "present_tier1":
            continue
        coverage["site_check"] = site_coverage
        if board_url(url):
            coverage['scope_mode'] = 'ezboard'
            scope = board_url(url)
        elif sitepowerup_board(url):
            coverage['scope_mode'] = 'sitepowerup'
            scope = sitepowerup_board(url)
        scopes.add(identity)
        identifier = digest(url)[:24]
        store.db.execute("INSERT OR IGNORE INTO candidates(id,url,scope,priority,coverage,evidence) VALUES (?,?,?,?,?,?)",
                         (identifier, url, scope, priority, json.dumps(coverage), json.dumps(evidence)))
        if board_url(url):
            from ezboard_discovery import evidence as parent_evidence
            captures = parent_evidence(store, linked_url, url)
            if captures:
                store.db.execute("UPDATE candidates SET captures=?,state='sampled' WHERE id=? AND captures='[]'", (json.dumps(captures), identifier))
        added += 1
    store.db.commit()
    store.set('ezboard_pending_links', unresolved)
    store.set('ezboard_coverage_pending', coverage_pending)
    store.set("discovery_limits", vars(args) | {"handler": None})
    store.set("discovery_result", {"seed_reads": reads, "seed_bytes": read_bytes, "seed_failures": failures, "seed_probes": probes,
                                   "distinct_linked_urls": len(grouped), "candidates": len(store.candidates())})
    print(json.dumps(store.get("discovery_result")), flush=True)
