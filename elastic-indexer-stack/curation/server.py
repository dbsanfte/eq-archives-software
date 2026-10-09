"""Unauthenticated LAN review service with bounded, source-bound mutations."""

from contextlib import asynccontextmanager
import ipaddress
import json
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from common import CrawlError, capture_scope, digest, now, site_identity
from review import apply_decisions, checked_sources, queue, record
from captures import LIMITS, TEXT_LIMIT, check_manifest, document, readable, verified_path
from state import Lane, active_operation, connect, enqueue, operation_lane, unpack, valid_id
from worker import Worker
from coverage_check import refresh, require_new
from capture_flow import Action, UNDO_SECONDS, transition
from capture_queue import claim, queue_order
from capture_continuation import start as continue_capture, require_complete
from site_reviews import get as get_site_review, migrate
from jobs import import_attempt, import_name
from portal import Stage, decorate, counts, search_matches, candidate_filter, dismissal_preview, grade_value
from manual import existing_site, site_url
from spending import Spending
from candidate_actions import dismiss_all, undo_dismissal
from candidate_checks import attach_checks, require_finished, start as start_candidate_check
from automatic_indexing import retry as retry_preparation
from grading import criteria
from review_actions import (preview as review_preview, decide_all as decide_all_reviews,
                            apply_decision as apply_site_decision, validate_decision as validate_site_decision,
                            undo_dismissal as undo_review_dismissal)

NETWORK = ipaddress.ip_network("192.168.0.0/16")
STATIC = Path(__file__).parent
CAPTURED_STATES = ('captured_awaiting_review','approved_waiting_publication','indexing_declined','published','indexed')


class Intranet:
    def __init__(self, app, origin):
        self.app, self.origin = app, origin
        self.host = origin.split("://", 1)[1]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            allowed = ipaddress.ip_address(scope["client"][0]) in NETWORK
        except (ValueError, TypeError):
            allowed = False
        headers = dict(scope["headers"])
        if not allowed:
            return await PlainTextResponse("Intranet access only", status_code=403)(scope, receive, send)
        if headers.get(b"host", b"").decode() != self.host:
            return await PlainTextResponse("Unrecognized host", status_code=403)(scope, receive, send)
        origin = headers.get(b"origin")
        if origin and origin.decode() != self.origin:
            return await PlainTextResponse("Same-origin access required", status_code=403)(scope, receive, send)
        if scope["method"] not in ("GET", "HEAD"):
            if (origin != self.origin.encode() or headers.get(b"x-curation-request") != b"1"
                    or headers.get(b"content-type", b"").split(b";", 1)[0] != b"application/json"):
                return await PlainTextResponse("Same-origin JSON action required", status_code=403)(scope, receive, send)
        async def secured(message):
            if message["type"] == "http.response.start":
                message["headers"].extend([
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'none'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ])
            await send(message)
        return await self.app(scope, receive, secured)


async def body(request):
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > 65536:
            raise CrawlError("Action exceeds the request limit")
    try:
        return json.loads(raw)
    except ValueError:
        raise CrawlError("Action must contain valid JSON") from None


def create_app(root=None, origin=None, start_worker=True):
    root = Path(root or os.environ.get("CURATION_ROOT", "/data"))
    origin = origin or os.environ.get("CURATION_ORIGIN", "http://192.168.50.100:8090")
    with connect(root):
        pass
    migrate(root)
    spending = Spending(root)

    @asynccontextmanager
    async def lifespan(app):
        worker = Worker(root) if start_worker else None
        app.state.worker = worker
        if worker:
            worker.start()
        try:
            yield
        finally:
            if worker:
                from starlette.concurrency import run_in_threadpool
                await run_in_threadpool(worker.close)

    def health(request):
        worker = getattr(request.app.state, "worker", None)
        healthy = not start_worker or worker and worker.healthy()
        return JSONResponse({"status": "ok" if healthy else "unavailable", "version": os.environ.get("GIT_SHA", "development")},
                            status_code=200 if healthy else 503)

    def screen(request):
        return FileResponse(STATIC / "review.html", media_type="text/html")

    def asset(request):
        filename = request.path_params["name"]
        if filename not in ("review.js", "review.css", "theme.js"):
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(STATIC / filename, media_type="text/javascript" if filename.endswith("js") else "text/css")

    def listing(request):
        refresh(root)
        try:
            offset = int(request.query_params.get("offset", "0"))
            if offset < 0:
                raise ValueError()
        except ValueError:
            raise CrawlError("Invalid queue offset") from None
        with connect(root) as store:
            all_rows = attach_checks(store, decorate(store, queue(store)))
            selected = request.query_params.get("filter", "recommended")
            if selected not in ("recommended", "pending", "approved", "captured", "review", "all", *Stage):
                raise CrawlError("Invalid queue filter")
            search = request.query_params.get('search', '').strip()
            if len(search) > 200:
                raise CrawlError('Site search exceeds 200 characters')
            rows = [row for row in all_rows if selected == row['stage'] or selected == "all" or
                    selected == "recommended" and row["state"] in ("approval_pending", "deferred")
                    and (row["rating"] or {}).get("grade", -1) >= 2 or
                    selected == "pending" and row["state"] in ("approval_pending", "deferred") or
                    selected == "approved" and row["state"] == 'approved_waiting_batch' or
                    selected == "review" and row.get('review_state') == 'awaiting_review' or
                    selected == "captured" and row['state'] in CAPTURED_STATES]
            if selected == 'candidates':
                if request.query_params.get('needs_grade', '0') not in ('0', '1'):
                    raise CrawlError('Invalid needs-grading filter')
                rows = candidate_filter(rows, request.query_params.get('min_grade'), request.query_params.get('needs_grade') == '1')
            if selected in ('approved', 'queued'):
                positions = {candidate: index for index, candidate in enumerate(queue_order(store))}
                rows.sort(key=lambda row: positions[row['id']])
            elif selected in ('captured', 'review', 'indexing', 'history'):
                rows.sort(key=lambda row: ((row['coverage'] or {}).get('capture') or {}).get('completed_at', ''), reverse=True)
            if search:
                rows = [row for row in rows if search_matches(row, search)]
            operations = [unpack(row) for row in store.db.execute("SELECT * FROM operations ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'interrupted' THEN 2 ELSE 3 END,created DESC LIMIT 20")]
            workers = {lane.value: unpack(active) if (active := active_operation(store, lane)) else None for lane in Lane}
            fields = 'id,state,manifest_sha256,publication,job,error,created,updated' if request.query_params.get('compact') == '1' else '*'
            batches = [unpack(row) for row in store.db.execute(f"SELECT {fields} FROM batches WHERE state!='capture_group' ORDER BY created DESC LIMIT 100")]
            page_rows = rows[offset:offset + 50]
            return JSONResponse({"candidates": page_rows, "total": len(rows), "offset": offset,
                                 "stage_counts": counts(all_rows),
                                 "candidate_grades": {str(grade): sum(row['stage'] == Stage.CANDIDATES and grade_value(row) == grade for row in all_rows) for grade in range(-1, 4)},
                                 "dismissal": dismissal_preview(all_rows),
                                 "review_actions": review_preview(store, all_rows) if selected == 'review' else None,
                                 "luna_spend": spending.snapshot(),
                                 "all_count": len(all_rows), "approved": sum(row["state"] == "approved_waiting_batch" for row in all_rows),
                                 "recommended": sum(row['state'] in ('approval_pending','deferred') and (row['rating'] or {}).get('grade',-1)>=2 for row in all_rows),
                                 "operations": operations, "workers": workers, "batches": batches, "limits": LIMITS, "undo_seconds": UNDO_SECONDS,
                                 "capturing": sum(row['state']=='capturing' for row in all_rows),
                                 "captured": sum(row['state'] in CAPTURED_STATES for row in all_rows),
                                 "awaiting_site_review": store.db.execute("SELECT COUNT(*) FROM batches WHERE state='awaiting_review'").fetchone()[0],
                                 "capture_queue_error": store.get('capture_queue_error'),
                                 "version": os.environ.get("GIT_SHA", "development"), "last_campaign": store.get("last_campaign"),
                                 "pilot_grading": store.get("grading_result")})

    def candidate_detail(request):
        with connect(root) as store:
            row = store.db.execute('SELECT * FROM candidates WHERE id=?', (request.query_params.get('id'),)).fetchone()
            if not row:
                raise CrawlError('Unknown candidate')
            candidate = attach_checks(store, decorate(store, [record(store, row)]))[0]
            capture = (candidate.get('coverage') or {}).get('capture') or {}
            review_id = capture.get('review_id') or capture.get('batch_id')
            review = get_site_review(store, review_id, candidate['id'], request.query_params.get('known_manifest')) if review_id and candidate['stage'] != Stage.CAPTURING else None
            operation = store.db.execute("""SELECT * FROM operations WHERE kind='capture' AND EXISTS
                (SELECT 1 FROM json_each(operations.payload,'$.sites') WHERE json_extract(value,'$.id')=?)
                ORDER BY created DESC,rowid DESC LIMIT 1""", (candidate['id'],)).fetchone()
            ids = queue_order(store)
            blocker = active_operation(store, paused_capture=True)
            return JSONResponse({'candidate': candidate, 'review': review,
                'capture_operation': unpack(operation) if operation else None,
                'queue_position': ids.index(candidate['id']) + 1 if candidate['id'] in ids else None,
                'queue_blocker': {key: blocker[key] for key in ('id', 'kind', 'state')} if blocker else None,
                'capture_queue_error': store.get('capture_queue_error')})

    async def restore_candidate(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) != {'id','manifest_sha256'}
                or not all(isinstance(value, str) for value in payload.values())):
            raise CrawlError('Restore requires the candidate ID and reviewed manifest hash')
        refresh(root)
        with connect(root) as store:
            store.db.execute('BEGIN IMMEDIATE')
            row = store.db.execute('SELECT * FROM candidates WHERE id=?',(payload['id'],)).fetchone()
            if not row or record(store,row)['manifest_sha256'] != payload['manifest_sha256']:
                raise CrawlError('Candidate changed since review')
            if json.loads(row['coverage'] or '{}').get('ezboard_parent_required'):
                raise CrawlError('Submit the top-level Ezboard URL to resolve its board identity before restoring a candidate.')
            from common import candidate_exclusion
            if reason := candidate_exclusion(row['url']):
                raise CrawlError(reason)
            state = transition(row['state'], Action.RESTORE)
            store.db.execute('UPDATE candidates SET state=?,decision=NULL WHERE id=?',(state,row['id']))
            store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                             (row['id'],'candidate_restored',json.dumps({'previous_state':row['state']}),now()))
            store.db.commit()
        return JSONResponse({'state':state})

    def source(request):
        try:
            slot = int(request.query_params.get("slot", "0"))
            if slot < 0:
                raise ValueError()
        except ValueError:
            raise CrawlError("Invalid source slot") from None
        with connect(root) as store:
            batch_id = request.query_params.get("batch")
            if batch_id:
                row = store.db.execute("SELECT manifest FROM batches WHERE id=?", (valid_id(batch_id),)).fetchone()
                if not row:
                    raise CrawlError("Unknown batch")
                captures = json.loads(row[0])["captures"]
                if slot >= len(captures):
                    raise CrawlError("Unknown source slot")
                capture = captures[slot]
                if request.query_params.get('download') == '1':
                    return FileResponse(verified_path(root, capture), media_type='application/octet-stream',
                                        filename=Path(capture['archive_path']).name)
                if readable(capture) and capture['bytes'] <= TEXT_LIMIT:
                    page, _ = document(root, capture)
                    result = {"complete_extracted_text": "\n".join(page.text), **capture}
                else:
                    verified_path(root, capture)
                    result = {**capture, 'complete_extracted_text': None,
                              'file_message': 'Original file preserved. Download it or open its dated Wayback capture.'}
            else:
                row = store.db.execute("SELECT * FROM candidates WHERE id=?", (request.query_params.get("candidate"),)).fetchone()
                if not row:
                    raise CrawlError("Unknown candidate")
                sources = checked_sources(store, record(store, row))
                if slot >= len(sources):
                    raise CrawlError("Unknown source slot")
                result = sources[slot]
            return JSONResponse(result)

    def site_review(request):
        with connect(root) as store:
            return JSONResponse(get_site_review(store,request.query_params.get('id'),request.query_params.get('candidate')))

    async def site_decision(request):
        payload = await body(request)
        if (not isinstance(payload,dict) or set(payload) != {'id','manifest_sha256','decision'}
                or not all(isinstance(value,str) for value in payload.values())
                or payload['decision'] not in ('approve','decline','reconsider')):
            raise CrawlError('Site decision requires the review ID, manifest hash and approve/decline/reconsider')
        def decide():
            if payload['decision'] == 'approve':
                refresh(root)
            with connect(root) as store:
                reviewed = get_site_review(store,payload['id'])
                if reviewed['manifest_sha256'] != payload['manifest_sha256']:
                    raise CrawlError('Captured site changed since review')
                validate_site_decision(store, reviewed, payload['decision'])
                if payload['decision'] == 'approve':
                    require_new(store,reviewed['manifest']['sites'][0]['url'])
                    # Large source reads stay outside the event loop and write
                    # lock, so status polling and worker progress can continue.
                    check_manifest(root,reviewed['manifest'])
                store.db.execute('BEGIN IMMEDIATE')
                current = get_site_review(store,payload['id'])
                if current['manifest_sha256'] != payload['manifest_sha256']:
                    raise CrawlError('Captured site changed while sources were verified')
                result = apply_site_decision(store, current, payload['decision'])
                store.db.commit()
                return result
        result = await run_in_threadpool(decide)
        return JSONResponse({key: result[key] for key in ('state', 'operation')},status_code=202 if result['operation'] else 200)

    async def review_decisions(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) != {'token', 'decision'}
                or not isinstance(payload['token'], str) or payload['decision'] not in ('approve', 'decline')):
            raise CrawlError('Bulk review requires the confirmed snapshot and approve/decline decision.')
        result = await run_in_threadpool(decide_all_reviews, root, payload['token'], payload['decision'])
        return JSONResponse(result, status_code=202 if payload['decision'] == 'approve' else 200)

    async def capture_continuation(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) != {'id', 'manifest_sha256'}
                or not all(isinstance(value, str) for value in payload.values())):
            raise CrawlError('Continue capture requires the review ID and manifest hash')
        def continue_review():
            refresh(root)
            return continue_capture(root, payload['id'], payload['manifest_sha256'])
        return JSONResponse(await run_in_threadpool(continue_review), status_code=202)

    async def undo_review_decisions(request):
        payload = await body(request)
        if not isinstance(payload, dict) or set(payload) != {'dismissal'}:
            raise CrawlError('Undo requires a review dismissal ID.')
        result = await run_in_threadpool(undo_review_dismissal, root, valid_id(payload['dismissal']))
        return JSONResponse(result)

    async def decide(request):
        incoming = await body(request)
        refresh(root)
        with connect(root) as store:
            apply_decisions(store, incoming, capture_delay=UNDO_SECONDS, before_approve=require_finished)
        return JSONResponse({"recorded": len(incoming)})

    async def dismiss_candidates(request):
        payload = await body(request)
        if not isinstance(payload, dict) or set(payload) != {'token'} or not isinstance(payload['token'], str):
            raise CrawlError('Dismiss all requires the reviewed Candidates snapshot.')
        with connect(root) as store:
            result = dismiss_all(store, payload['token'])
        return JSONResponse(result)

    async def undo_candidates(request):
        payload = await body(request)
        if not isinstance(payload, dict) or set(payload) != {'dismissal'}:
            raise CrawlError('Undo requires a dismissal ID.')
        refresh(root)
        with connect(root) as store:
            result = undo_dismissal(store, valid_id(payload['dismissal']))
        return JSONResponse(result)

    async def check_candidate(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) - {'grading_criteria'} != {'id', 'manifest_sha256', 'max_usd'}
                or not all(isinstance(payload[key], str) for key in ('id', 'manifest_sha256'))
                or type(payload['max_usd']) not in (int, float) or not 0 < payload['max_usd'] <= 2):
            raise CrawlError('Evidence and grading require the candidate hash and an explicit one-site budget up to $2.')
        if 'grading_criteria' in payload:
            payload['grading_criteria'] = criteria(payload['grading_criteria'])
        with connect(root) as store:
            result = start_candidate_check(store, payload)
        return JSONResponse(result, status_code=202)

    async def index_retry(request):
        payload = await body(request)
        if (not isinstance(payload,dict) or set(payload) != {'id','manifest_sha256','job_name'}
                or not all(isinstance(value,str) for value in payload.values())):
            raise CrawlError('Indexing retry requires the site review ID, manifest hash and failed Job name')
        with connect(root) as store:
            store.db.execute('BEGIN IMMEDIATE')
            reviewed = get_site_review(store,payload['id'])
            if (reviewed['state'] != 'index_failed' or reviewed['manifest_sha256'] != payload['manifest_sha256']
                    or (reviewed['job'] or {}).get('name') != payload['job_name']
                    or import_name(reviewed) != payload['job_name']):
                raise CrawlError('This indexing failure changed or is already queued for retry')
            # Revalidate the original published approval and every source. Never
            # republish, mutate a terminal Job or change the approved budget.
            from index_captures import read_batch
            published = read_batch(root,f"batches/{reviewed['id']}/approved.json",reviewed['manifest_sha256'])
            if published['publication']['commit'] != (reviewed['publication'] or {}).get('commit'):
                raise CrawlError('Published approval changed before indexing retry')
            job = {'attempt':import_attempt(reviewed)+1,'previous_name':payload['job_name'],'state':'retry_queued'}
            import_name({**reviewed,'job':job})  # Validate the next immutable Job identity.
            store.db.execute("UPDATE batches SET state='published_waiting_index',job=?,error=NULL,updated=? WHERE id=?",
                             (json.dumps(job),now(),reviewed['id']))
            store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                             (reviewed['manifest']['sites'][0]['id'],'site_indexing_retried',json.dumps(payload),now()))
            store.db.commit()
        return JSONResponse({'state':'published_waiting_index','attempt':job['attempt']},status_code=202)

    async def prepare_retry(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) != {'id', 'manifest_sha256'}
                or not all(isinstance(value, str) for value in payload.values())):
            raise CrawlError('Preparation retry requires the captured site ID and manifest hash')
        result = await run_in_threadpool(retry_preparation, root, payload['id'], payload['manifest_sha256'])
        return JSONResponse(result, status_code=202)

    async def undo(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) != {'id','manifest_sha256'}
                or not all(isinstance(value, str) for value in payload.values())):
            raise CrawlError('Undo requires the candidate ID and reviewed manifest hash')
        with connect(root) as store:
            store.db.execute('BEGIN IMMEDIATE')
            row = store.db.execute('SELECT * FROM candidates WHERE id=?', (payload['id'],)).fetchone()
            if not row or record(store,row)['manifest_sha256'] != payload['manifest_sha256']:
                raise CrawlError('Candidate changed since review')
            current = record(store, row)
            if current['coverage'].get('capture', {}).get('continuation'):
                from capture_continuation import undo as undo_continuation
                state = undo_continuation(store, current)
                store.db.commit()
                return JSONResponse({'state': state})
            state = transition(row['state'], Action.UNDO)
            store.db.execute('UPDATE candidates SET state=?,decision=NULL WHERE id=?', (state,row['id']))
            store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                             (row['id'], 'approval_undone', json.dumps({'previous_decision': json.loads(row['decision'])}), now()))
            store.db.commit()
        return JSONResponse({'state':state})

    async def scope_change(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) not in ({"id", "manifest_sha256", "mode"}, {"id", "manifest_sha256", "mode", "path"})
                or not all(isinstance(payload[key], str) for key in payload)):
            raise CrawlError("Scope changes require a candidate ID, manifest hash and scope mode")
        if (payload['mode'] == 'custom') != ('path' in payload):
            raise CrawlError('Custom scope requires an explicit folder path')
        refresh(root)
        with connect(root) as store:
            store.db.execute("BEGIN IMMEDIATE")
            row = store.db.execute("SELECT * FROM candidates WHERE id=?", (payload["id"],)).fetchone()
            if row and json.loads(row['coverage'] or '{}').get('capture', {}).get('continuation'):
                raise CrawlError('Undo queued regeneration before changing its approved capture scope')
            if not row or row["state"] not in ("approval_pending", "approved_waiting_batch", "deferred", "rejected",
                                               'sample_error','unavailable','identity_unresolved','sampled','grade_error','discovered'):
                raise CrawlError("Candidate cannot change scope in its current state")
            current = record(store, row)
            if current["manifest_sha256"] != payload["manifest_sha256"]:
                raise CrawlError("Candidate changed since review")
            scope = capture_scope(current["url"], payload["mode"], payload.get('path'))
            coverage = current["coverage"] or {}
            coverage["scope_mode"] = payload["mode"]
            store.db.execute("UPDATE candidates SET scope=?,coverage=?,decision=NULL,state='approval_pending' WHERE id=?", (scope, json.dumps(coverage), payload["id"]))
            store.db.commit()
            store.event(payload["id"], "scope_changed", {"scope": scope, "mode": payload["mode"]})
        return JSONResponse({"scope": scope})

    async def coverage_check(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) != {'id', 'manifest_sha256'}
                or not all(isinstance(value, str) for value in payload.values())):
            raise CrawlError('Coverage recheck requires the reviewed candidate and manifest hash')
        with connect(root) as store:
            row = store.db.execute('SELECT * FROM candidates WHERE id=?', (payload['id'],)).fetchone()
            if not row or record(store,row)['manifest_sha256'] != payload['manifest_sha256']:
                raise CrawlError('Candidate changed since review')
        if not os.environ.get('ARCHIVE_REPO'):
            raise CrawlError('Archive metadata is not configured; coverage cannot be verified')
        refresh(root, candidate_id=payload['id'], force=True)
        with connect(root) as store:
            row = store.db.execute('SELECT * FROM candidates WHERE id=?', (payload['id'],)).fetchone()
            current = record(store, row)
        return JSONResponse({'checked': payload['id'], 'state': current['state'],
                             'coverage': (current['coverage'] or {}).get('site_check')})

    async def discovery(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) - {'grading_criteria'} not in ({"max_candidates", "max_usd"}, {"max_candidates", "max_usd", "min_grade"})
                or type(payload["max_candidates"]) is not int or not 1 <= payload["max_candidates"] <= 50
                or type(payload["max_usd"]) not in (int, float) or not 0 < payload["max_usd"] <= 2
                or type(payload.get("min_grade", 2)) is not int or payload.get("min_grade", 2) not in range(4)):
            raise CrawlError("Discovery requires explicit bounds: 1–50 candidates and up to $2")
        payload['grading_criteria'] = criteria(payload.get('grading_criteria', ''))
        with connect(root) as store:
            operation = enqueue(store, "discover", {**payload, "min_grade": payload.get("min_grade", 2), "fill_queue": True})
        return JSONResponse({"operation": operation}, status_code=202)

    async def submit_site(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) - {'grading_criteria'} != {'url', 'max_usd'}
                or type(payload['max_usd']) not in (int, float) or not 0 < payload['max_usd'] <= 2):
            raise CrawlError('A site submission requires a URL and an explicit budget of up to $2')
        url = site_url(payload['url'])
        focus = criteria(payload.get('grading_criteria', ''))
        with connect(root) as store:
            store.db.execute('BEGIN IMMEDIATE')
            existing = existing_site(store, url)
            if existing is not None:
                return JSONResponse({'candidate_id': existing['id'], 'url': existing['url'], 'existing': True})
            for row in store.db.execute("SELECT * FROM operations WHERE kind='discover' AND state IN ('queued','running','interrupted') ORDER BY created"):
                operation = unpack(row)
                target = operation['payload'].get('target')
                if target and site_identity(target['url'], store) == site_identity(url, store):
                    return JSONResponse({'operation': operation['id'], 'url': target['url'], 'existing': True})
            operation = enqueue(store, 'discover', {'max_candidates': 1, 'max_usd': payload['max_usd'],
                'grading_criteria': focus,
                'target': {'url': url, 'submitted_url': payload['url'].strip()}}, commit=False)
            store.db.commit()
        return JSONResponse({'operation': operation, 'url': url}, status_code=202)

    async def capture(request):
        payload = await body(request)
        ids = payload.get("ids") if isinstance(payload, dict) and set(payload) == {"ids"} else None
        if (not isinstance(ids, list) or not 1 <= len(ids) <= LIMITS["sites"]
                or not all(isinstance(value, str) for value in ids) or len(set(ids)) != len(ids)):
            raise CrawlError("Select 1–5 approved candidates for a capture batch")
        refresh(root)
        with connect(root) as store:
            store.db.execute("BEGIN IMMEDIATE")
            operation, batch_id = claim(store, ids)
            store.db.commit()
        return JSONResponse({"operation": operation, "batch": batch_id}, status_code=202)

    async def publication(request):
        payload = await body(request)
        if not isinstance(payload, dict) or set(payload) not in ({"id", "manifest_sha256"}, {"id", "manifest_sha256", "slots"}):
            raise CrawlError("Publication requires the reviewed batch ID and manifest hash")
        with connect(root) as store:
            current = store.db.execute('SELECT manifest FROM batches WHERE id=?', (valid_id(payload['id']),)).fetchone()
            if current and current['manifest']:
                for site in json.loads(current['manifest'])['sites']:
                    require_new(store, site['url'])
            store.db.execute("BEGIN IMMEDIATE")
            row = store.db.execute("SELECT * FROM batches WHERE id=?", (valid_id(payload["id"]),)).fetchone()
            if not row:
                raise CrawlError("Unknown batch")
            batch = unpack(row)
            if batch["state"] != "awaiting_review" or batch["manifest_sha256"] != payload["manifest_sha256"]:
                raise CrawlError("Batch changed since review or is already approved")
            manifest = batch["manifest"]
            require_complete(manifest)
            if len(manifest['sites']) != 1:
                raise CrawlError('Review and approve one captured site at a time')
            slots = payload.get("slots", list(range(len(manifest["captures"]))))
            if (not isinstance(slots, list) or not slots or not all(type(slot) is int and 0 <= slot < len(manifest["captures"]) for slot in slots)
                    or len(set(slots)) != len(slots)):
                raise CrawlError("Select at least one capture from the reviewed file set")
            if len(slots) != len(manifest["captures"]):
                if manifest.get('capture_policy') == 'complete-files-v1':
                    raise CrawlError('Complete file captures require whole-site approval; choose every captured file')
                manifest = {**manifest, "captures": [capture for index, capture in enumerate(manifest["captures"]) if index in slots],
                            "reviewed_manifest_sha256": batch["manifest_sha256"]}
            check_manifest(root, manifest)
            approved_hash = digest(manifest)
            operation = enqueue(store, "publish", {"batch_id":payload["id"], "manifest_sha256":approved_hash}, commit=False)
            store.db.execute("UPDATE batches SET state='publication_requested',manifest=?,manifest_sha256=?,updated=? WHERE id=?",
                             (json.dumps(manifest), approved_hash, now(), payload["id"]))
            store.db.execute("INSERT INTO events(candidate,action,detail,created) VALUES (NULL,'publication_approved',?,?)", (json.dumps(payload), now()))
            store.db.commit()
        return JSONResponse({"operation": operation}, status_code=202)

    async def resume(request):
        payload = await body(request)
        if not isinstance(payload, dict) or set(payload) != {"id"}:
            raise CrawlError("Resume requires an operation ID")
        with connect(root) as store:
            store.db.execute("BEGIN IMMEDIATE")
            operation = store.db.execute("SELECT kind FROM operations WHERE id=? AND state='interrupted'", (valid_id(payload['id']),)).fetchone()
            if not operation:
                raise CrawlError("Operation is not awaiting an explicit resume")
            if active_operation(store, operation_lane(operation['kind'])):
                raise CrawlError("Another operation for this worker is queued or running")
            result = store.db.execute("UPDATE operations SET state='queued',error=NULL,updated=? WHERE id=? AND state='interrupted'", (now(), valid_id(payload["id"])))
            if result.rowcount != 1:
                raise CrawlError("Operation is not awaiting an explicit resume")
            store.db.commit()
        return JSONResponse({"resumed": payload["id"]}, status_code=202)

    async def problem(request, error):
        return JSONResponse({"error": str(error)}, status_code=409)

    app = Starlette(routes=[Route("/", screen), Route("/assets/{name}", asset), Route("/healthz", health),
                            Route("/api/queue", listing), Route("/api/source", source),
                            Route('/api/candidate',candidate_detail), Route('/api/restore',restore_candidate,methods=['POST']),
                            Route('/api/site',site_review), Route('/api/site-decision',site_decision,methods=['POST']),
                            Route('/api/continue-capture',capture_continuation,methods=['POST']),
                            Route('/api/review-decisions',review_decisions,methods=['POST']),
                            Route('/api/undo-review-dismissal',undo_review_decisions,methods=['POST']),
                            Route('/api/index-retry',index_retry,methods=['POST']),
                            Route('/api/prepare-indexing',prepare_retry,methods=['POST']),
                            Route("/api/decisions", decide, methods=["POST"]),
                            Route('/api/dismiss-candidates', dismiss_candidates, methods=['POST']),
                            Route('/api/undo-dismissal', undo_candidates, methods=['POST']),
                            Route('/api/check-candidate', check_candidate, methods=['POST']),
                            Route("/api/undo", undo, methods=["POST"]), Route("/api/scope", scope_change, methods=["POST"]), Route('/api/coverage', coverage_check, methods=['POST']), Route("/api/discover", discovery, methods=["POST"]),
                            Route('/api/submit-site', submit_site, methods=['POST']),
                            Route("/api/capture", capture, methods=["POST"]), Route("/api/publish", publication, methods=["POST"]),
                            Route("/api/resume", resume, methods=["POST"])], lifespan=lifespan,
                    exception_handlers={CrawlError: problem})
    app.add_middleware(Intranet, origin=origin)
    return app


def main():
    import uvicorn
    address = os.environ.get("LAN_BIND_IP", "192.168.50.100")
    if ipaddress.ip_address(address) not in NETWORK:
        raise SystemExit("Curation must bind an address in 192.168.0.0/16")
    os.environ["CURATION_ORIGIN"] = f"http://{address}:8090"
    uvicorn.run(create_app(), host=address, port=8090, proxy_headers=False, access_log=False,
                log_level="warning", limit_concurrency=16, timeout_keep_alive=5,
                timeout_graceful_shutdown=110, h11_max_incomplete_event_size=16384)


if __name__ == "__main__":
    main()
