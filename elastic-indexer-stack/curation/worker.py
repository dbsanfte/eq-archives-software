"""Independent resumable candidate, capture and publication/indexing workers."""

import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from common import CrawlError, Store, capture_scope, digest, now, save
from campaign_storage import initialize as initialize_campaign
from capture_flow import Action, UNDO_SECONDS, transition
from capture_queue import claim, claim_resume, next_entry
from capture_rank import PROBE_INTERVAL, measure_next
from crawler import parser
from discovery import discover
from acquisition import Downloader, sample
from wayback_transport import SharedWayback
from grading import grade
from graph import staged_links
from captures import capture_sites
from capture_progress import CaptureProgress
from jobs import Kubernetes, blockers, finished, import_attempt, import_job, import_name, reindex_blockers
from publisher import cleanup_preparations, prepare, publish
from publication_queue import SMALL_SLOTS, claim as claim_publication, current as current_publication
from state import Lane, OPERATION_KINDS, active_operation, connect, database_busy, unpack, worker_lease
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
from daily_budget import PORTAL_PRIORITY, automatic_request, require_automatic

LOG=logging.getLogger(__name__)


class ImportQueueUnavailable(CrawlError):
    """Job creation had an uncertain outcome and cannot safely advance yet."""


def campaign(root, operation):
    target = operation['payload'].get('target')
    if target:
        with connect(root) as main:
            existing = existing_site(main, target['url'])
            if existing is not None:
                return {'candidate_id': existing['id'], 'existing': True, 'candidates': 0}
    directory = Path(root) / "runs" / operation["id"]
    initialize_campaign(root, directory)
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
            args = options.parse_args(["--work-dir", str(directory), "sample", "--selection-context", "--max-candidates", str(payload["max_candidates"]),
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
        cleanup_preparations(self.root)
        self.transport = SharedWayback(self.stop)
        self.daily_budget = automation.budget(self.root)
        from ezboard_portal import consolidate
        consolidate(self.root)
        refresh(self.root)
        migrate(self.root)
        with connect(self.root) as store:
            # Approved downloads recover on a timer. Interrupted paid discovery
            # keeps its original limits; owned publication failures recover below.
            from capture_failures import WORKER_INTERRUPTED
            store.db.execute("""UPDATE operations SET state='interrupted',
                error=CASE WHEN kind='capture' THEN ? ELSE 'Worker stopped; resume explicitly' END,
                updated=? WHERE state='running'""", (WORKER_INTERRUPTED, now()))
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
        self.index_wake = threading.Event()
        self.publication_wake = threading.Event()
        self.index_thread = threading.Thread(target=self.run_index_jobs, name='curation-index-jobs', daemon=True)
        self.rank_thread = threading.Thread(target=self.run_capture_ranking, name='curation-capture-ranking', daemon=True)
        self.prepared = {True: Queue(SMALL_SLOTS), False: Queue(1)}
        self.preparers = tuple(threading.Thread(target=self.run_publication_preparation, args=(small,),
                               name=f'curation-publication-{size}', daemon=True)
                               for small, size in ((True, 'small'), (False, 'large')))

    def start(self):
        for thread in (*self.threads.values(), self.index_thread, self.rank_thread, *self.preparers):
            thread.start()

    def healthy(self):
        return not self.stop.is_set() and self.transport.healthy() and all(
            thread.is_alive() for thread in (*self.threads.values(), self.index_thread, *self.preparers))

    def close(self):
        self.stop.set()
        self.transport.wake()
        self.index_wake.set()
        self.publication_wake.set()
        deadline = time.monotonic() + 100
        threads = (*self.threads.values(), self.index_thread, self.rank_thread, *self.preparers)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        # Never hand ownership to another process while a download or Git
        # request is still in flight. Process exit releases a retained lease.
        if not any(thread.is_alive() for thread in threads):
            for queue in self.prepared.values():
                while not queue.empty():
                    queue.get_nowait()[1].close()
            self.transport.close()
            self.lease.close()

    def save_state(self, write, *args):
        """Retry a metadata transaction, never the external work it records.

        Each callback opens/closes its own connection and commits last. A busy
        transaction rolls back on close before retry; shutdown leaves unfinished
        work for the existing checkpoint/reconciliation rules.
        """
        while True:
            try:
                return write(*args)
            except sqlite3.OperationalError as error:
                if not database_busy(error):raise
                LOG.warning('Curation metadata write waiting for SQLite; retrying without replaying external work')
                if self.stop.wait(1):raise

    def publication_complete(self, operation, result, progress, manifest):
        batch_id = manifest['batch_id']
        with connect(self.root) as store:
            store.db.execute("DELETE FROM meta WHERE key='publication_backoff'")
            store.db.execute("UPDATE batches SET state='published_waiting_index',publication=?,updated=? WHERE id=?",
                             (json.dumps(result), now(), batch_id))
            for site in manifest['sites']:
                row=store.db.execute('SELECT state FROM candidates WHERE id=?',(site['id'],)).fetchone()
                store.db.execute('UPDATE candidates SET state=? WHERE id=?',(transition(row['state'],Action.PUBLISH),site['id']))
            store.db.execute("UPDATE operations SET state='completed',result=?,updated=? WHERE id=?",
                             (json.dumps({**result, 'progress':progress}), now(), operation['id']))
            store.db.commit()
        self.index_wake.set()

    def publication_progress(self, operation, progress):
        with connect(self.root) as store:
            store.db.execute("UPDATE operations SET result=?,updated=? WHERE id=? AND state='running'",
                             (json.dumps({'batch_id':operation['payload']['batch_id'], 'progress':progress}),
                              now(), operation['id']))
            store.db.commit()

    def run_publication_preparation(self, small):
        while not self.stop.is_set():
            try:
                if self.prepare_publication_turn(small):
                    continue
            except sqlite3.OperationalError as error:
                if not database_busy(error):
                    raise
                LOG.warning('Publication preparation waiting for SQLite')
            self.stop.wait(3)

    def prepare_publication_turn(self, small):
        ready = self.prepared[small]
        if ready.full():
            return
        operation = claim_publication(self.root, small)
        if operation is None:
            prepare_next(self.root, small=small)
            operation = claim_publication(self.root, small)
        if operation is None:
            return
        try:
            batch_id = operation['payload']['batch_id']
            expected = operation['payload']['manifest_sha256']
            with connect(self.root) as store:
                if not current_publication(store, operation):
                    raise CrawlError('Publication approval is no longer current')
                manifest = json.loads(store.db.execute('SELECT manifest FROM batches WHERE id=?', (batch_id,)).fetchone()[0])
                if manifest['batch_id'] != batch_id:
                    raise CrawlError('Publication batch does not match its manifest')
                for site in manifest['sites']:
                    require_eligible(store, site)
            state = {'total':len(manifest['captures']), 'seconds':{}}
            previous = time.monotonic()

            def progress(phase, files=None):
                nonlocal previous
                when = time.monotonic()
                if state.get('phase') != phase:
                    if 'phase' in state:
                        seconds = state['seconds']
                        seconds[state['phase']] = round(seconds.get(state['phase'], 0) + when - previous, 2)
                    previous = when
                state['phase'] = phase
                if files is not None:
                    state['files'] = files
                if phase != 'completed':
                    self.save_state(self.publication_progress, operation, state)
                return state

            budget_identity(self.root, manifest)
            prepared = prepare(self.root, manifest, expected, progress)
            try:
                progress('ready')
                ready.put_nowait((operation, prepared, progress))
                self.publication_wake.set()
            except BaseException:
                prepared.close()
                raise
        except Exception as error:
            self.save_state(self.operation_interrupted, operation, error)
        return True

    def push_publications(self):
        from publication_recovery import paused
        if paused(self.root):
            return
        try:
            # One large ready site gets a turn ahead of smaller batches.
            batch = [self.prepared[False].get_nowait()]
        except Empty:
            batch = []
            for _ in range(SMALL_SLOTS):
                try:
                    batch.append(self.prepared[True].get_nowait())
                except Empty:
                    break
        eligible = {}
        try:
            for operation, site, progress in batch:
                with connect(self.root) as store:
                    approved = current_publication(store, operation)
                if approved:
                    eligible[site.identifier] = (operation, site, progress)
                else:
                    self.save_state(self.operation_interrupted, operation, CrawlError('Publication approval is no longer current'))
            if not eligible:
                return
            shared_failure = False
            try:
                receipts = publish(self.root, [site for _, site, _ in eligible.values()],
                                   lambda site, phase: eligible[site.identifier][2](phase))
            except Exception as error:
                shared_failure = True
                receipts = dict.fromkeys(eligible, error)
            for batch_id, (operation, site, progress) in eligible.items():
                try:
                    result = receipts[batch_id]
                    if isinstance(result, Exception):
                        raise result
                    destination = self.root / 'batches' / batch_id / 'approved.json'
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    save(destination, {'manifest':site.manifest, 'manifest_sha256':site.expected, 'publication':result})
                    self.save_state(self.publication_complete, operation, result, progress('completed'), site.manifest)
                except Exception as error:
                    self.save_state(self.operation_interrupted, operation, error, not shared_failure or batch_id == next(iter(eligible)))
        finally:
            for _, site, _ in batch:
                site.close()

    def capture_progress(self, operation, batch_id, snapshot):
        with connect(self.root) as store:
            store.db.execute("UPDATE operations SET result=?,updated=? WHERE id=? AND state='running'",
                             (json.dumps({'batch_id':batch_id,'progress':snapshot}),now(),operation['id']))
            store.db.commit()

    def capture_complete(self, batch_id, manifest):
        with connect(self.root) as store:
            store.db.execute("UPDATE batches SET state='awaiting_review',manifest=?,manifest_sha256=?,error=NULL,updated=? WHERE id=?",
                             (json.dumps(manifest),digest(manifest),now(),batch_id))
            for site in manifest['sites']:
                row=store.db.execute('SELECT state,coverage FROM candidates WHERE id=?',(site['id'],)).fetchone()
                coverage=json.loads(row['coverage'] or '{}')
                coverage['capture']={'batch_id':batch_id,'completed_at':now()}
                store.db.execute('UPDATE candidates SET state=?,coverage=? WHERE id=?',
                                 (transition(row['state'],Action.COMPLETE),json.dumps(coverage),site['id']))
            materialize(store,unpack(store.db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()))
            store.db.commit()

    def operation_complete(self, operation, result):
        with connect(self.root) as store:
            store.db.execute("UPDATE operations SET state='completed',result=?,updated=? WHERE id=?",
                             (json.dumps(result),now(),operation['id']))
            store.db.commit()
        automation.finished(self.root,operation,result)

    def operation_interrupted(self, operation, error, shared_backoff=True):
        from storage_recovery import diagnostic, message, register as register_storage
        failure = diagnostic(error)
        detail = message(failure) if failure else str(error) if isinstance(error,CrawlError) else f'Operation failed ({type(error).__name__}); staged evidence retained'
        LOG.error('Curation operation %s failed: %s', operation['id'],
                  json.dumps(failure or {'category': type(error).__name__}))
        if operation['kind']=='capture' and detail=='Worker stopped; resume explicitly':
            from capture_failures import WORKER_INTERRUPTED
            detail=WORKER_INTERRUPTED
        with connect(self.root) as store:
            previous=store.db.execute('SELECT result FROM operations WHERE id=?',(operation['id'],)).fetchone()
            result=json.loads(previous['result'] or '{}');result.pop('automatic_pause',None)
            if failure:
                result['failure'] = failure
                register_storage(store, operation, failure)
            else:
                result.pop('failure', None)
            if pause:=automation.pause_detail(error,operation):result['automatic_pause']=pause
            store.db.execute("UPDATE operations SET state='interrupted',error=?,result=?,updated=? WHERE id=?",
                             (detail,json.dumps(result),now(),operation['id']))
            if operation['kind']=='publish':
                from publication_recovery import register
                register(store,operation,error,shared_backoff=shared_backoff)
            if operation['kind']=='capture':
                from capture_recovery import register
                register(store,operation,detail)
            store.db.commit()

    def capture_queue(self):
        from capture_recovery import schedule
        schedule(self.root)
        with connect(self.root) as store:
            due = next_entry(store, now())
            if not due:
                if store.get('capture_queue_error'):
                    store.set('capture_queue_error', None)
                return
        refresh(self.root)
        with connect(self.root) as store:
            store.db.execute('BEGIN IMMEDIATE')
            if active_operation(store):
                store.db.rollback()
                return
            entry = next_entry(store, now())
            if not entry:
                store.db.rollback()
                return
            if entry['operation']:
                claim_resume(store, entry['operation'])
                store.db.commit()
                return
            # Claim only the site about to start. Later approvals remain queued
            # and undoable throughout an earlier site's download.
            store.db.execute('SAVEPOINT capture_claim')
            try:
                claim(store, [entry['id']])
            except Exception as error:
                # Retain the approval and Undo; do not re-read broken sources
                # every tick or prevent another site from being claimed.
                store.db.execute('ROLLBACK TO capture_claim')
                detail = str(error) if isinstance(error, CrawlError) else 'Capture preparation failed; saved approval and sources are retained.'
                store.db.execute('INSERT OR REPLACE INTO capture_queue_failures VALUES (?,?,?,?)',
                                 (entry['id'], entry['decision'], detail, now()))
            store.db.execute('RELEASE capture_claim')
            store.db.commit()
            if store.get('capture_queue_error'):
                store.set('capture_queue_error', None)

    def operation(self, lane=Lane.CAPTURE):
        if lane==Lane.INDEXING:
            self.push_publications()
            return
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
                    self.save_state(self.capture_progress,operation,batch_id,latest)
                manifest = capture_sites(self.root, batch_id, operation["payload"]["sites"], progress=progress)
                self.save_state(self.capture_complete,batch_id,manifest)
                result = {"captures": len(manifest["captures"]), "batch_id": batch_id, "progress": latest}
            self.save_state(self.operation_complete,operation,result)
        except Exception as error:
            self.save_state(self.operation_interrupted,operation,error)
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
            batches = [unpack(row) for row in store.db.execute("SELECT id,state,manifest_sha256,job,publication FROM batches INDEXED BY batch_metadata WHERE state IN ('published_waiting_index','indexing','index_budget_waiting') ORDER BY created")]
        maintenance = os.environ.get('PASSAGE_MAINTENANCE_ENABLED') == '1'
        reenrichment = os.environ.get('PASSAGE_REENRICHMENT_ENABLED') == '1'
        from policy_scheduler import ENRICHMENT_STATE, import_pending, schedule, write_control
        if not batches and not (maintenance or reenrichment):
            write_control(self.root, False)
            self.daily_budget.prioritize(PORTAL_PRIORITY, False)
            return
        self.kube = self.kube or Kubernetes()
        jobs = self.kube.jobs()
        write_control(self.root, maintenance, import_running=bool(blockers(jobs, '')))
        # Reconcile and admit the next archive slice before admitting any new
        # import. The same snapshot includes jobs submitted earlier in this turn.
        uncertain = []
        paid_pending = False
        for enabled, state_key, paid in ((maintenance, 'policy_maintenance', False),
                                        (reenrichment, ENRICHMENT_STATE, True)):
            if not enabled:
                continue
            try:
                state = schedule(self.root, self.kube, jobs, os.environ.get('IMPORT_IMAGE', ''), enrichment=paid)
                if state['phase'] == 'checking_submission':
                    uncertain.append(state['name'])
                if paid:
                    paid_pending = state['phase'] in ('running', 'checking_submission')
            except Exception as error:
                if database_busy(error):
                    raise
                with connect(self.root) as store:
                    saved = store.get(state_key, {})
                    store.set(state_key, {**saved, 'phase': 'retry_wait',
                              'error': 'Archive maintenance scheduling is unavailable. It will retry.'})
        reindexing = sorted(set(reindex_blockers(jobs)) | set(uncertain))
        for batch in batches:
            try:
                self.index_batch(batch, jobs, reindexing=reindexing)
            except ImportQueueUnavailable:
                raise
            except Exception as error:
                if database_busy(error):raise
                detail = str(error) if isinstance(error, CrawlError) else 'Index submission failed for this site; saved sources and paid results are retained.'
                job = dict(batch.get('job') or {})
                try:
                    job.update(name=import_name(batch), attempt=import_attempt(batch), state='submission_failed')
                except CrawlError:
                    pass
                with connect(self.root) as store:
                    store.db.execute("UPDATE batches SET state='index_failed',job=?,error=?,updated=? WHERE id=?",
                                     (json.dumps(job), detail, now(), batch['id']))
                    store.db.commit()
        self.daily_budget.prioritize(PORTAL_PRIORITY, paid_pending or import_pending(self.root))

    def index_batch(self, batch, jobs, *, reindexing=()):
        import_error = None
        name = import_name(batch)
        attempt_detail = {"attempt": import_attempt(batch)}
        if (batch.get('job') or {}).get('previous_name'):
            attempt_detail['previous_name'] = batch['job']['previous_name']
        existing = next((job for job in jobs if job["metadata"]["name"] == name), None)
        if existing:
            if finished(existing) and 'manifest' not in batch:
                with connect(self.root) as store:
                    batch={**batch,'manifest':json.loads(store.db.execute('SELECT manifest FROM batches WHERE id=?',(batch['id'],)).fetchone()[0])}
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
            if (state == 'index_failed'
                    and any(c.get('type') == 'Failed' and c.get('status') == 'True' and c.get('reason') == 'DeadlineExceeded'
                            for c in existing.get('status', {}).get('conditions', []))
                    and (read_status(self.root, batch, name) or {}).get('state') != 'failed'):
                # Execution limits apply to every approved import, including old
                # manual $2 sites. Keep the immutable policy/cache identity; a
                # timeout never renews its budget or overrides a saved failure.
                state, detail = 'published_waiting_index', self.continue_import(batch, name)
                detail['recovery_reason'] = 'Import execution deadline reached; continuing saved work under the original enrichment budget.'
            if state == 'index_failed':
                import_error = import_error or read_error(self.root, batch, name)
                if not import_error and hasattr(self.kube,'job_failure'):
                    try:import_error=self.kube.job_failure(existing)
                    except CrawlError:import_error='Import Job failed; pod diagnostics are temporarily unavailable. Saved sources and paid results are retained.'
                if (import_error and 'OOMKilled' in import_error
                        and existing['spec']['template']['spec']['containers'][0].get('resources',{}).get('limits',{}).get('memory')=='512Mi'):
                    state,detail='published_waiting_index',self.continue_import(batch,name)
                    detail['recovery_reason']='Previous 512 MiB importer was too small for the pinned tokenizer; retry uses the current memory profile.'
                    import_error=None
        else:
            if batch['state'] == 'index_budget_waiting':
                raise CrawlError('Budget-paused import Job is unavailable; automatic continuation is paused')
            waiting = blockers(jobs, name)
            if reindexing:
                state, detail = "published_waiting_index", {**attempt_detail, "waiting_for_reindex": reindexing}
            elif waiting:
                state, detail = "published_waiting_index", {**attempt_detail, "waiting_for": waiting}
            else:
                (self.root / "enrichment").mkdir(exist_ok=True, mode=0o700)
                job = import_job(batch, os.environ.get("IMPORT_IMAGE", ""))
                try:
                    self.kube.create(job)
                except Exception:
                    # A lost create response may still have started a Job. Re-list
                    # before advancing; never bypass foreground admission.
                    try:
                        jobs[:] = self.kube.jobs()
                    except Exception as error:
                        raise ImportQueueUnavailable('Index submission outcome is uncertain; waiting for Kubernetes state before continuing.') from error
                    if not any(item['metadata']['name'] == name for item in jobs):
                        raise
                state, detail = "indexing", {**attempt_detail, "name": name, "state": "submitted"}
                # No further submissions until the next list sees this Job.
                if not any(item['metadata']['name'] == name for item in jobs):
                    jobs.append({"metadata": {"name": name,"labels":{"app":"eqarchives-capture-import"}}, "spec": {}, "status": {}})
        with connect(self.root) as store:
            job_json = json.dumps(detail)
            store.db.execute("""UPDATE batches SET state=?,job=?,error=?,updated=? WHERE id=?
                AND (state IS NOT ? OR job IS NOT ? OR error IS NOT ?)""",
                (state, job_json, import_error, now(), batch["id"], state, job_json, import_error))
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
                self.run_turn(lane)
            except sqlite3.OperationalError as error:
                if not database_busy(error):raise
                LOG.warning('Curation %s worker waiting for SQLite; queued work retained for next turn',lane.value)
                self.stop.wait(1)
                continue
            if lane == Lane.INDEXING:
                if self.publication_wake.wait(10):
                    self.publication_wake.clear()
                    self.stop.wait(.5)  # Collect sites that finish preparation together.
            else:
                self.stop.wait(10)

    def run_index_jobs(self):
        # The same process lease owns this sole Job reconciler. Publication can
        # spend minutes on Git without holding completion checks/submissions.
        with self.daily_budget.bind():
            while not self.stop.is_set():
                try:
                    self.indexing_turn()
                except sqlite3.OperationalError as error:
                    if not database_busy(error):raise
                    LOG.warning('Curation import reconciliation waiting for SQLite; queued work retained')
                self.index_wake.wait(10)
                self.index_wake.clear()

    def run_capture_ranking(self):
        while not self.stop.wait(PROBE_INTERVAL):
            try:
                measure_next(self.root, self.transport)
            except (sqlite3.Error, OSError):
                LOG.warning('Capture size check paused; current queue ranks are retained')

    def indexing_turn(self):
        # This control loop stays free while the capture executor downloads a
        # large site. A due retry joins the ranked queue without preempting it.
        from capture_recovery import schedule
        try:
            schedule(self.root)
        except Exception as error:
            if database_busy(error):raise
            LOG.warning('Capture retry scheduling paused; retained approvals need attention')
        try:
            self.indexing()
        except Exception as error:
            if database_busy(error):raise
            detail = str(error) if isinstance(error, CrawlError) else "Index submission paused; Kubernetes state unavailable"
            with connect(self.root) as store:
                store.db.execute("UPDATE batches SET error=? WHERE state IN ('published_waiting_index','indexing','index_budget_waiting')", (detail,))
                store.db.commit()

    def run_turn(self, lane):
        from storage_recovery import schedule as retry_storage
        retry_storage(self.root, lane)
        try:
            automation.resume_owned(self.root, lane, self.daily_budget)
            if lane == Lane.CANDIDATES:
                automation.promote(self.root, self.daily_budget)
                automation.schedule(self.root, self.daily_budget)
        except Exception as error:
            if database_busy(error):raise
            with connect(self.root) as store:
                detail = str(error) if isinstance(error, CrawlError) else 'Automatic scheduling paused; saved state is retained'
                store.set('automatic_activity', {'phase': 'attention', 'error': detail})
        if lane == Lane.INDEXING:
            from publication_recovery import schedule
            schedule(self.root)
        if lane == Lane.CAPTURE:
            try:
                self.capture_queue()
            except Exception as error:
                if database_busy(error):raise
                # Keep stale approvals queued for an explicit correction.
                with connect(self.root) as store:
                    detail = str(error) if isinstance(error, CrawlError) else 'Capture queue paused; staging is unavailable'
                    if store.get('capture_queue_error') != detail:
                        store.set('capture_queue_error', detail)
        self.operation(lane)
        if self.stop.is_set():
            return
