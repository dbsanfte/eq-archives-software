"""Independent resumable candidate, capture and publication/indexing workers."""

import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from common import CrawlError, Store, capture_scope, digest, now, save, site_scope
from capture_flow import Action, UNDO_SECONDS, transition
from capture_queue import claim
from crawler import parser
from discovery import discover
from acquisition import Downloader, sample
from wayback_transport import SharedWayback
from grading import grade
from graph import staged_links
from captures import capture_sites
from capture_progress import CaptureProgress
from jobs import Kubernetes, blockers, import_attempt, import_job, import_name
from publisher import publish
from state import Lane, OPERATION_KINDS, active_operation, connect, unpack, worker_lease
from coverage_check import refresh
from site_reviews import materialize, migrate
from import_status import read_error, read_status, read_budget_pause
from manual import existing_site, prepare as prepare_manual
from candidate_checks import run as check_candidate
from discovery_run import fill, retain_ezboard_progress
from automatic_indexing import prepare_next
from capture_budget import budget_identity
from capture_continuation import require_eligible
import automation
from daily_budget import automatic_request, require_automatic


def campaign(root, operation):
    target = operation['payload'].get('target')
    if target:
        with connect(root) as main:
            existing = existing_site(main, target['url'])
            if existing is not None:
                return {'candidate_id': existing['id'], 'existing': True, 'candidates': 0}
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
            run.db.execute("DELETE FROM meta WHERE key IN ('acquisition_started','wayback_transport','luna_budget_usd','grading_result', 'discovery_completed','sampling_completed','discovery_result','sampling_result','staged_graph_result')")
            run.db.execute("DELETE FROM meta WHERE key LIKE 'ezboard_resolution:%' OR key='ezboard_pending_links'")
            run.set("excluded_scopes", excluded)
        finally:
            run.close()
        (directory / "initialized").touch(mode=0o600)
    run = Store(directory)
    downloader = None
    try:
        payload = operation["payload"]
        if payload.get("fill_queue"):
            return fill(root, operation, run)
        options = parser()
        from ezboard import candidate_url
        if target and candidate_url(run, target['url']) is None:
            args = options.parse_args(['--work-dir', str(directory), 'sample', '--max-candidates', '1', '--max-seconds', '1800'])
            downloader = Downloader(run, args)
        acquire = prepare_manual(run, operation, downloader=downloader) if target else True
        if target:
            with connect(root) as main:
                existing = existing_site(main, run.candidates()[0]['url'])
                if existing:
                    main.db.executemany('INSERT OR IGNORE INTO ezboard_aliases VALUES (?,?,?,?)',
                                        [tuple(row) for row in run.db.execute('SELECT * FROM ezboard_aliases')])
                    main.db.commit()
                    return {'candidate_id': existing['id'], 'existing': True, 'candidates': 0}
        if not target and not run.get("discovery_completed"):
            run.set("staged_graph_result", staged_links(root, run))
            args = options.parse_args(["--work-dir", str(directory), "discover", "--archive-repo", os.environ.get("ARCHIVE_REPO", "/archive"),
                                       "--max-candidates", str(payload["max_candidates"])])
            discover(args, run)
            run.set("discovery_completed", True)
        if acquire and not run.get("sampling_completed"):
            args = options.parse_args(["--work-dir", str(directory), "sample", "--max-candidates", str(payload["max_candidates"]),
                                       "--max-seconds", "1800", "--retry-unresolved"])
            if downloader is None:
                sample(args, run)
            else:
                sample(args, run, downloader=downloader)
            run.set("sampling_completed", True)
        args = options.parse_args(["--work-dir", str(directory), "grade", "--api-key-file", "/run/secrets/luna_api_key",
                                   "--max-candidates", str(payload["max_candidates"]), "--max-usd", str(payload["max_usd"])])
        args.grading_criteria = payload.get('grading_criteria', '')
        if acquire:
            grade(args, run)
        with connect(root) as main:
            main.db.execute("ATTACH DATABASE ? AS campaign", (str(directory / "crawl.sqlite3"),))
            for table in ("hosts", "tree_state", "files", "scans", "links", "ezboard_aliases", "ezboard_archive_boards", "ezboard_archive_forums", "sitepowerup_archive_boards"):
                main.db.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM campaign.{table}")
            retain_ezboard_progress(main, run)
            for row in run.candidates():
                record = dict(row)
                record["scope"] = capture_scope(record["url"], json.loads(record['coverage'] or '{}').get('scope_mode', 'directory'))
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
        return {"candidates": len(run.candidates()), "grading": run.get("grading_result"), "staged_graph": run.get("staged_graph_result"),
                **({'candidate_id': run.candidates()[0]['id']} if target else {})}
    finally:
        if downloader is not None:
            downloader.close()
        run.close()


class Worker:
    def __init__(self, root, kube=None):
        self.root, self.kube = Path(root), kube
        self.stop = threading.Event()
        self.lease = worker_lease(self.root)
        self.transport = SharedWayback(self.stop)
        self.daily_budget = automation.budget(self.root)
        from ezboard_portal import consolidate
        consolidate(self.root)
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
        # One process lease owns all workers and the shared Wayback connection.
        # Existing operation kinds select
        # their queue, without migrating payloads, checkpoints or approvals.
        self.threads = {lane: threading.Thread(target=self.run, args=(lane,),
                        name=f"curation-{lane.value}", daemon=True) for lane in Lane}

    def start(self):
        for thread in self.threads.values():
            thread.start()

    def healthy(self):
        return not self.stop.is_set() and self.transport.healthy() and all(thread.is_alive() for thread in self.threads.values())

    def close(self):
        self.stop.set()
        self.transport.wake()
        deadline = time.monotonic() + 100
        for thread in self.threads.values():
            if thread.ident is not None:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        # Never hand ownership to another process while a download or Git
        # request is still in flight. Process exit releases a retained lease.
        if not any(thread.is_alive() for thread in self.threads.values()):
            self.transport.close()
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
            if active_operation(store, paused_capture=True):
                store.db.rollback()
                return
            rows = store.db.execute("SELECT id FROM candidates WHERE state='approved_waiting_batch' AND json_extract(decision,'$.capture_after')<=? ORDER BY json_extract(decision,'$.capture_after'),rowid LIMIT 1", (now(),)).fetchall()
            if not rows:
                store.db.rollback()
                return
            # Claim only the site about to start. Later approvals remain queued
            # and undoable throughout an earlier site's download.
            claim(store, [row['id'] for row in rows])
            store.db.commit()
            if store.get('capture_queue_error'):
                store.set('capture_queue_error', None)

    def operation(self, lane=Lane.CAPTURE):
        kinds = OPERATION_KINDS[Lane(lane)]
        with connect(self.root) as store:
            store.db.execute("BEGIN IMMEDIATE")
            if active_operation(store, lane, states=('running',)):
                store.db.rollback()
                return
            row = store.db.execute(f"SELECT * FROM operations WHERE state='queued' AND kind IN ({','.join('?' for _ in kinds)}) ORDER BY created,rowid LIMIT 1", kinds).fetchone()
            if not row:
                store.db.rollback()
                return
            operation = unpack(row)
            store.db.execute("UPDATE operations SET state='running',error=NULL,updated=? WHERE id=?", (now(), operation["id"]))
            store.db.commit()
        try:
            auto_token = automatic_request.set(bool(operation['payload'].get('automatic')) and lane == Lane.CANDIDATES)
            require_automatic()
            if operation["kind"] == "discover":
                result = campaign(self.root, operation)
            elif operation['kind'] == 'candidate_check':
                result = check_candidate(self.root, operation)
            elif operation["kind"] == "publish":
                batch_id = operation["payload"]["batch_id"]
                with connect(self.root) as store:
                    batch = unpack(store.db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone())
                expected = operation["payload"]["manifest_sha256"]
                if batch["state"] != "publication_requested" or batch["manifest_sha256"] != expected:
                    raise CrawlError("Publication approval is no longer current")
                with connect(self.root) as store:
                    for site in batch['manifest']['sites']:
                        require_eligible(store, site)
                budget_identity(self.root, batch['manifest'])
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
                        require_eligible(store, site)
                batch_id = operation["payload"]["batch_id"]
                latest = None
                tracker = CaptureProgress()
                def progress(snapshot):
                    nonlocal latest
                    latest = tracker.update(snapshot)
                    with connect(self.root) as store:
                        store.db.execute("UPDATE operations SET result=?,updated=? WHERE id=? AND state='running'",
                                         (json.dumps({'batch_id': batch_id, 'progress': latest}), now(), operation['id']))
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
            automation.finished(self.root, operation, result)
        except Exception as error:
            detail = str(error) if isinstance(error, CrawlError) else f"Operation failed ({type(error).__name__}); staged evidence retained"
            with connect(self.root) as store:
                previous = store.db.execute('SELECT result FROM operations WHERE id=?', (operation['id'],)).fetchone()
                result = json.loads(previous['result'] or '{}')
                if pause := automation.pause_detail(error, operation):
                    result['automatic_pause'] = pause
                store.db.execute("UPDATE operations SET state='interrupted',error=?,result=?,updated=? WHERE id=?", (detail, json.dumps(result), now(), operation["id"]))
                store.db.commit()
        finally:
            automatic_request.reset(auto_token)

    def continue_import(self, batch, name):
        from index_captures import read_batch
        published = read_batch(self.root, f"batches/{batch['id']}/approved.json", batch['manifest_sha256'])
        if published['publication']['commit'] != batch['publication']['commit']:
            raise CrawlError('Published approval changed before automatic import continuation')
        budget_identity(self.root, published['manifest'])
        detail = {'attempt': import_attempt(batch) + 1, 'previous_name': name, 'state': 'automatic_resume_queued'}
        import_name({**batch, 'job': detail})
        return detail

    def indexing(self):
        with connect(self.root) as store:
            batches = [unpack(row) for row in store.db.execute("SELECT * FROM batches WHERE state IN ('published_waiting_index','indexing','index_budget_waiting') ORDER BY created")]
        if not batches:
            return
        self.kube = self.kube or Kubernetes()
        jobs = self.kube.jobs()
        for batch in batches:
            import_error = None
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
                daily_job = any(item.get('name') == 'LUNA_DAILY_BUDGET_ROOT' for item in existing.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [{}])[0].get('env', []))
                if state == 'indexed' and daily_job:
                    diagnostic = read_status(self.root, batch, name) or {}
                    pause = read_budget_pause(self.root, batch, name)
                    if pause:
                        state, detail = 'index_budget_waiting', {**detail, 'state': 'waiting_budget', **pause}
                        if self.daily_budget.snapshot()['remaining_usd'] >= pause['required_usd']:
                            state, detail = 'published_waiting_index', self.continue_import(batch, name)
                    elif diagnostic.get('state') != 'completed':
                        state = 'index_failed'
                        import_error = 'Import completion could not be verified; saved sources and paid results are retained.'
                if (state == 'index_failed' and daily_job and batch['manifest'].get('indexing', {}).get('daily_budget') == 'portal-utc-v1'
                        and any(c.get('type') == 'Failed' and c.get('status') == 'True' and c.get('reason') == 'DeadlineExceeded'
                                for c in existing.get('status', {}).get('conditions', []))):
                    # Large automatic sites can span the six-hour Job window.
                    # Continue using caches/create-only IDs, retaining old Jobs.
                    state, detail = 'published_waiting_index', self.continue_import(batch, name)
                if state == 'index_failed':
                    import_error = import_error or read_error(self.root, batch, name)
            else:
                if batch['state'] == 'index_budget_waiting':
                    raise CrawlError('Budget-paused import Job is unavailable; automatic continuation is paused')
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
                store.db.execute("UPDATE batches SET state=?,job=?,error=?,updated=? WHERE id=?",
                                 (state, json.dumps(detail), import_error, now(), batch["id"]))
                if state == "indexed":
                    for site in batch["manifest"]["sites"]:
                        row = store.db.execute('SELECT state,coverage FROM candidates WHERE id=?', (site['id'],)).fetchone()
                        capture = json.loads(row['coverage'] or '{}').get('capture', {})
                        current = capture.get('review_id') or capture.get('batch_id')
                        # Finishing an earlier import must not retire a site
                        # whose missing files are being retried in a new batch.
                        if row['state'] == 'published' and (not current or current == batch['id']):
                            store.db.execute('UPDATE candidates SET state=? WHERE id=?', (transition(row['state'], Action.INDEX), site['id']))
                store.db.commit()

    def run(self, lane):
        with self.transport.bind(), self.daily_budget.bind():
            self.run_lane(lane)

    def run_lane(self, lane):
        while not self.stop.is_set():
            try:
                automation.resume_owned(self.root, lane, self.daily_budget)
                if lane == Lane.CANDIDATES:
                    automation.promote(self.root, self.daily_budget)
                    automation.schedule(self.root, self.daily_budget)
            except Exception as error:
                with connect(self.root) as store:
                    detail = str(error) if isinstance(error, CrawlError) else 'Automatic scheduling paused; saved state is retained'
                    store.set('automatic_activity', {'phase': 'attention', 'error': detail})
            if lane == Lane.INDEXING:
                prepare_next(self.root)
            if lane == Lane.CAPTURE:
                try:
                    self.capture_queue()
                except Exception as error:
                    # Keep stale approvals queued for an explicit correction.
                    with connect(self.root) as store:
                        detail = str(error) if isinstance(error, CrawlError) else 'Capture queue paused; staging is unavailable'
                        if store.get('capture_queue_error') != detail:
                            store.set('capture_queue_error', detail)
            self.operation(lane)
            if self.stop.is_set():
                break
            if lane == Lane.INDEXING:
                try:
                    self.indexing()
                except Exception as error:
                    detail = str(error) if isinstance(error, CrawlError) else "Index submission paused; Kubernetes state unavailable"
                    with connect(self.root) as store:
                        store.db.execute("UPDATE batches SET error=? WHERE state IN ('published_waiting_index','indexing','index_budget_waiting')", (detail,))
                        store.db.commit()
            self.stop.wait(10)
