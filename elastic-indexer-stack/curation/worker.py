"""One resumable worker for discovery, staged acquisition and targeted indexing."""

import json
import os
from pathlib import Path
import sqlite3
import threading
from datetime import datetime, timedelta

from common import CrawlError, Store, capture_scope, digest, now, save, site_scope
from capture_flow import Action, UNDO_SECONDS, transition
from capture_queue import claim
from crawler import parser
from discovery import discover
from acquisition import sample
from grading import grade
from graph import staged_links
from captures import capture_sites
from jobs import Kubernetes, blockers, import_attempt, import_job, import_name
from publisher import publish
from state import connect, unpack, worker_lease
from coverage_check import refresh, require_new
from site_reviews import materialize, migrate


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
        refresh(self.root)
        migrate(self.root)
        with connect(self.root) as store:
            # A terminated paid/download request has uncertain outcome. Require
            # an explicit resume; retained dollar and HTTP reservations still apply.
            store.db.execute("UPDATE operations SET state='interrupted',error='Worker stopped; resume explicitly',updated=? WHERE state='running'", (now(),))
            # Give legacy approvals the same opportunity to undo on first rollout.
            for row in store.db.execute("SELECT id,decision FROM candidates WHERE state='approved_waiting_batch'").fetchall():
                grant = json.loads(row['decision'])
                if 'capture_after' not in grant:
                    grant['capture_after'] = (datetime.fromisoformat(now()) + timedelta(seconds=UNDO_SECONDS)).isoformat()
                    store.db.execute('UPDATE candidates SET decision=? WHERE id=?', (json.dumps(grant), row['id']))
            store.db.commit()
        self.thread = threading.Thread(target=self.run, name="curation-worker", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=100)
        self.lease.close()

    def capture_queue(self):
        with connect(self.root) as store:
            due = store.db.execute("SELECT 1 FROM candidates WHERE state='approved_waiting_batch' AND json_extract(decision,'$.capture_after')<=?", (now(),)).fetchone()
            if not due:
                if store.get('capture_queue_error'):
                    store.set('capture_queue_error', None)
                return
        refresh(self.root)
        with connect(self.root) as store:
            store.db.execute('BEGIN IMMEDIATE')
            if store.db.execute("SELECT 1 FROM operations WHERE state IN ('queued','running') OR kind='capture' AND state='interrupted'").fetchone():
                store.db.rollback()
                return
            rows = store.db.execute("SELECT id FROM candidates WHERE state='approved_waiting_batch' AND json_extract(decision,'$.capture_after')<=? ORDER BY json_extract(decision,'$.capture_after'),rowid LIMIT 5", (now(),)).fetchall()
            if not rows:
                store.db.rollback()
                return
            claim(store, [row['id'] for row in rows])
            store.db.commit()
            if store.get('capture_queue_error'):
                store.set('capture_queue_error', None)

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
                with connect(self.root) as store:
                    for site in batch['manifest']['sites']:
                        require_new(store, site['url'])
                result = publish(self.root, batch["manifest"], expected)
                destination = self.root / "batches" / batch_id / "approved.json"
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                save(destination,
                     {"manifest": batch["manifest"], "manifest_sha256": expected, "publication": result})
                with connect(self.root) as store:
                    store.db.execute("UPDATE batches SET state='published_waiting_index',publication=?,updated=? WHERE id=?",
                                     (json.dumps(result), now(), batch_id))
                    for site in batch["manifest"]["sites"]:
                        row = store.db.execute('SELECT state FROM candidates WHERE id=?', (site['id'],)).fetchone()
                        store.db.execute('UPDATE candidates SET state=? WHERE id=?', (transition(row['state'], Action.PUBLISH), site['id']))
                    store.db.commit()
            else:
                with connect(self.root) as store:
                    for site in operation['payload']['sites']:
                        require_new(store, site['url'])
                batch_id = operation["payload"]["batch_id"]
                latest = None
                def progress(snapshot):
                    nonlocal latest
                    latest = snapshot
                    with connect(self.root) as store:
                        store.db.execute("UPDATE operations SET result=?,updated=? WHERE id=? AND state='running'",
                                         (json.dumps({'batch_id': batch_id, 'progress': snapshot}), now(), operation['id']))
                        store.db.commit()
                manifest = capture_sites(self.root, batch_id, operation["payload"]["sites"], progress=progress)
                with connect(self.root) as store:
                    store.db.execute("UPDATE batches SET state='awaiting_review',manifest=?,manifest_sha256=?,error=NULL,updated=? WHERE id=?",
                                     (json.dumps(manifest), digest(manifest), now(), batch_id))
                    for site in manifest["sites"]:
                        row = store.db.execute('SELECT state,coverage FROM candidates WHERE id=?', (site['id'],)).fetchone()
                        coverage = json.loads(row['coverage'] or '{}')
                        coverage['capture'] = {'batch_id':batch_id,'completed_at':now()}
                        store.db.execute('UPDATE candidates SET state=?,coverage=? WHERE id=?',
                                         (transition(row['state'], Action.COMPLETE), json.dumps(coverage), site['id']))
                    materialize(store, unpack(store.db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()))
                    store.db.commit()
                result = {"captures": len(manifest["captures"]), "batch_id": batch_id, "progress": latest}
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
            name = import_name(batch)
            attempt_detail = {"attempt": import_attempt(batch)}
            if (batch.get('job') or {}).get('previous_name'):
                attempt_detail['previous_name'] = batch['job']['previous_name']
            existing = next((job for job in jobs if job["metadata"]["name"] == name), None)
            if existing:
                annotations = existing["metadata"].get("annotations", {})
                if annotations.get("eqarchives.org/manifest-sha256") != batch["manifest_sha256"]:
                    raise CrawlError("Import Job does not match the approved manifest")
                conditions = {c["type"]: c["status"] for c in existing.get("status", {}).get("conditions", [])}
                state = "indexed" if conditions.get("Complete") == "True" else "index_failed" if conditions.get("Failed") == "True" else "indexing"
                detail = {**attempt_detail, "name": name, "state": state}
            else:
                waiting = blockers(jobs, name)
                if waiting:
                    state, detail = "published_waiting_index", {**attempt_detail, "waiting_for": waiting}
                else:
                    (self.root / "enrichment").mkdir(exist_ok=True, mode=0o700)
                    self.kube.create(import_job(batch, os.environ.get("IMPORT_IMAGE", "")))
                    state, detail = "indexing", {**attempt_detail, "name": name, "state": "submitted"}
                    # No further submissions until the next list sees this Job.
                    jobs.append({"metadata": {"name": name}, "spec": {}, "status": {}})
            with connect(self.root) as store:
                store.db.execute("UPDATE batches SET state=?,job=?,error=NULL,updated=? WHERE id=?",
                                 (state, json.dumps(detail), now(), batch["id"]))
                if state == "indexed":
                    for site in batch["manifest"]["sites"]:
                        row = store.db.execute('SELECT state FROM candidates WHERE id=?', (site['id'],)).fetchone()
                        store.db.execute('UPDATE candidates SET state=? WHERE id=?', (transition(row['state'], Action.INDEX), site['id']))
                store.db.commit()

    def run(self):
        while not self.stop.is_set():
            try:
                self.capture_queue()
            except Exception as error:
                # Keep invalid/stale approvals queued for an explicit correction.
                with connect(self.root) as store:
                    detail = str(error) if isinstance(error, CrawlError) else 'Capture queue paused; staging is unavailable'
                    if store.get('capture_queue_error') != detail:
                        store.set('capture_queue_error', detail)
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
