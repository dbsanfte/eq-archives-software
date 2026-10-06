"""One resumable worker for discovery, staged acquisition and targeted indexing."""

import json
import os
from pathlib import Path
import sqlite3
import threading

from common import CrawlError, Store, capture_scope, digest, now, save, site_scope
from crawler import parser
from discovery import discover
from acquisition import sample
from grading import grade
from graph import staged_links
from captures import capture_sites
from jobs import Kubernetes, blockers, import_job
from publisher import publish
from state import connect, unpack, worker_lease


def campaign(root, operation):
    directory = Path(root) / "runs" / operation["id"]
    if not (directory / "initialized").exists():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with connect(root) as main:
            backup = sqlite3.connect(directory / "crawl.sqlite3")
            main.db.backup(backup)
            backup.close()
            excluded = [site_scope(row["url"]) for row in main.candidates()]
        run = Store(directory)
        try:
            for table in ("candidates", "attempts", "events", "operations", "batches"):
                run.db.execute(f"DELETE FROM {table}")
            run.db.execute("DELETE FROM meta WHERE key IN ('acquisition_started','wayback_transport','luna_budget_usd','grading_result')")
            run.set("excluded_scopes", excluded)
        finally:
            run.close()
        (directory / "initialized").touch(mode=0o600)
    run = Store(directory)
    try:
        payload = operation["payload"]
        options = parser()
        if not run.get("discovery_completed"):
            run.set("staged_graph_result", staged_links(root, run))
            args = options.parse_args(["--work-dir", str(directory), "discover", "--archive-repo", os.environ.get("ARCHIVE_REPO", "/archive"),
                                       "--max-candidates", str(payload["max_candidates"])])
            discover(args, run)
            run.set("discovery_completed", True)
        if not run.get("sampling_completed"):
            args = options.parse_args(["--work-dir", str(directory), "sample", "--max-candidates", str(payload["max_candidates"]),
                                       "--max-seconds", "1800", "--retry-unresolved"])
            sample(args, run)
            run.set("sampling_completed", True)
        args = options.parse_args(["--work-dir", str(directory), "grade", "--api-key-file", "/run/secrets/luna_api_key",
                                   "--max-candidates", str(payload["max_candidates"]), "--max-usd", str(payload["max_usd"])])
        grade(args, run)
        with connect(root) as main:
            main.db.execute("ATTACH DATABASE ? AS campaign", (str(directory / "crawl.sqlite3"),))
            for table in ("hosts", "tree_state", "files", "scans", "links"):
                main.db.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM campaign.{table}")
            for row in run.candidates():
                record = dict(row)
                record["scope"] = capture_scope(record["url"])
                captures = json.loads(record["captures"])
                for capture in captures:
                    capture["path"] = f"runs/{operation['id']}/" + capture["path"]
                record["captures"] = json.dumps(captures)
                keys = list(record)
                main.db.execute(f"INSERT OR IGNORE INTO candidates({','.join(keys)}) VALUES ({','.join('?' for key in keys)})",
                                [record[key] for key in keys])
            main.db.commit()
            main.set("last_campaign", {"id": operation["id"], "discovery": run.get("discovery_result"),
                                       "sampling": run.get("sampling_result"), "grading": run.get("grading_result")})
        return {"candidates": len(run.candidates()), "grading": run.get("grading_result"), "staged_graph": run.get("staged_graph_result")}
    finally:
        run.close()


class Worker:
    def __init__(self, root, kube=None):
        self.root, self.kube = Path(root), kube
        self.stop = threading.Event()
        self.lease = worker_lease(self.root)
        with connect(self.root) as store:
            # A terminated paid/download request has uncertain outcome. Require
            # an explicit resume; retained dollar and HTTP reservations still apply.
            store.db.execute("UPDATE operations SET state='interrupted',error='Worker stopped; resume explicitly',updated=? WHERE state='running'", (now(),))
            store.db.commit()
        self.thread = threading.Thread(target=self.run, name="curation-worker", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=100)
        self.lease.close()

    def operation(self):
        with connect(self.root) as store:
            store.db.execute("BEGIN IMMEDIATE")
            row = store.db.execute("SELECT * FROM operations WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                store.db.rollback()
                return
            operation = unpack(row)
            store.db.execute("UPDATE operations SET state='running',error=NULL,updated=? WHERE id=?", (now(), operation["id"]))
            store.db.commit()
        try:
            if operation["kind"] == "discover":
                result = campaign(self.root, operation)
            elif operation["kind"] == "publish":
                batch_id = operation["payload"]["batch_id"]
                with connect(self.root) as store:
                    batch = unpack(store.db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone())
                expected = operation["payload"]["manifest_sha256"]
                if batch["state"] != "publication_requested" or batch["manifest_sha256"] != expected:
                    raise CrawlError("Publication approval is no longer current")
                result = publish(self.root, batch["manifest"], expected)
                destination = self.root / "batches" / batch_id / "approved.json"
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                save(destination,
                     {"manifest": batch["manifest"], "manifest_sha256": expected, "publication": result})
                with connect(self.root) as store:
                    store.db.execute("UPDATE batches SET state='published_waiting_index',publication=?,updated=? WHERE id=?",
                                     (json.dumps(result), now(), batch_id))
                    for site in batch["manifest"]["sites"]:
                        store.db.execute("UPDATE candidates SET state='published' WHERE id=?", (site["id"],))
                    store.db.commit()
            else:
                manifest = capture_sites(self.root, operation["payload"]["batch_id"], operation["payload"]["sites"])
                batch_id = operation["payload"]["batch_id"]
                with connect(self.root) as store:
                    store.db.execute("UPDATE batches SET state='awaiting_review',manifest=?,manifest_sha256=?,error=NULL,updated=? WHERE id=?",
                                     (json.dumps(manifest), digest(manifest), now(), batch_id))
                    for site in manifest["sites"]:
                        store.db.execute("UPDATE candidates SET state='captured_awaiting_review' WHERE id=?", (site["id"],))
                    store.db.commit()
                result = {"captures": len(manifest["captures"]), "batch_id": batch_id}
            with connect(self.root) as store:
                store.db.execute("UPDATE operations SET state='completed',result=?,updated=? WHERE id=?", (json.dumps(result), now(), operation["id"]))
                store.db.commit()
        except Exception as error:
            detail = str(error) if isinstance(error, CrawlError) else f"Operation failed ({type(error).__name__}); staged evidence retained"
            with connect(self.root) as store:
                store.db.execute("UPDATE operations SET state='interrupted',error=?,updated=? WHERE id=?", (detail, now(), operation["id"]))
                store.db.commit()

    def indexing(self):
        with connect(self.root) as store:
            batches = [unpack(row) for row in store.db.execute("SELECT * FROM batches WHERE state IN ('published_waiting_index','indexing') ORDER BY created")]
        if not batches:
            return
        self.kube = self.kube or Kubernetes()
        jobs = self.kube.jobs()
        for batch in batches:
            name = "eqarchives-captures-" + batch["id"]
            existing = next((job for job in jobs if job["metadata"]["name"] == name), None)
            if existing:
                annotations = existing["metadata"].get("annotations", {})
                if annotations.get("eqarchives.org/manifest-sha256") != batch["manifest_sha256"]:
                    raise CrawlError("Import Job does not match the approved manifest")
                conditions = {c["type"]: c["status"] for c in existing.get("status", {}).get("conditions", [])}
                state = "indexed" if conditions.get("Complete") == "True" else "index_failed" if conditions.get("Failed") == "True" else "indexing"
                detail = {"name": name, "state": state}
            else:
                waiting = blockers(jobs, name)
                if waiting:
                    state, detail = "published_waiting_index", {"waiting_for": waiting}
                else:
                    (self.root / "enrichment").mkdir(exist_ok=True, mode=0o700)
                    self.kube.create(import_job(batch, os.environ.get("IMPORT_IMAGE", "")))
                    state, detail = "indexing", {"name": name, "state": "submitted"}
                    # No further submissions until the next list sees this Job.
                    jobs.append({"metadata": {"name": name}, "spec": {}, "status": {}})
            with connect(self.root) as store:
                store.db.execute("UPDATE batches SET state=?,job=?,error=NULL,updated=? WHERE id=?",
                                 (state, json.dumps(detail), now(), batch["id"]))
                if state == "indexed":
                    for site in batch["manifest"]["sites"]:
                        store.db.execute("UPDATE candidates SET state='indexed' WHERE id=?", (site["id"],))
                store.db.commit()

    def run(self):
        while not self.stop.is_set():
            self.operation()
            if self.stop.is_set():
                break
            try:
                self.indexing()
            except Exception as error:
                detail = str(error) if isinstance(error, CrawlError) else "Index submission paused; Kubernetes state unavailable"
                with connect(self.root) as store:
                    store.db.execute("UPDATE batches SET error=? WHERE state IN ('published_waiting_index','indexing')", (detail,))
                    store.db.commit()
            self.stop.wait(10)
