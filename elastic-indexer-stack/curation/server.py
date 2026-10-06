"""Unauthenticated LAN review service with bounded, source-bound mutations."""

from contextlib import asynccontextmanager
import ipaddress
import json
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from common import CrawlError, capture_scope, digest, now, original_url, within_scope
from review import apply_decisions, checked_sources, queue, record
from captures import LIMITS, check_manifest, document
from state import connect, enqueue, identifier, unpack, valid_id
from worker import Worker

NETWORK = ipaddress.ip_network("192.168.0.0/16")
STATIC = Path(__file__).parent


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
        healthy = not start_worker or worker and worker.thread.is_alive()
        return JSONResponse({"status": "ok" if healthy else "unavailable", "version": os.environ.get("GIT_SHA", "development")},
                            status_code=200 if healthy else 503)

    def screen(request):
        return FileResponse(STATIC / "review.html", media_type="text/html")

    def asset(request):
        filename = request.path_params["name"]
        if filename not in ("review.js", "review.css"):
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(STATIC / filename, media_type="text/javascript" if filename.endswith("js") else "text/css")

    def listing(request):
        try:
            offset = int(request.query_params.get("offset", "0"))
            if offset < 0:
                raise ValueError()
        except ValueError:
            raise CrawlError("Invalid queue offset") from None
        with connect(root) as store:
            all_rows = queue(store)
            selected = request.query_params.get("filter", "recommended")
            if selected not in ("recommended", "pending", "all"):
                raise CrawlError("Invalid queue filter")
            rows = [row for row in all_rows if selected == "all" or
                    selected == "recommended" and row["state"] in ("approval_pending", "approved_waiting_batch", "deferred")
                    and (row["rating"] or {}).get("grade", -1) >= 2 or
                    selected == "pending" and row["state"] in ("approval_pending", "approved_waiting_batch", "deferred")]
            operations = [unpack(row) for row in store.db.execute("SELECT * FROM operations ORDER BY created DESC LIMIT 20")]
            batches = [unpack(row) for row in store.db.execute("SELECT * FROM batches ORDER BY created DESC LIMIT 100")]
            return JSONResponse({"candidates": rows[offset:offset + 50], "total": len(rows), "offset": offset,
                                 "all_count": len(all_rows), "approved": sum(row["state"] == "approved_waiting_batch" for row in all_rows),
                                 "operations": operations, "batches": batches, "limits": LIMITS,
                                 "version": os.environ.get("GIT_SHA", "development"), "last_campaign": store.get("last_campaign"),
                                 "pilot_grading": store.get("grading_result")})

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
                page, _ = document(root, capture)
                result = {"complete_extracted_text": "\n".join(page.text), **capture}
            else:
                row = store.db.execute("SELECT * FROM candidates WHERE id=?", (request.query_params.get("candidate"),)).fetchone()
                if not row:
                    raise CrawlError("Unknown candidate")
                sources = checked_sources(store, record(store, row))
                if slot >= len(sources):
                    raise CrawlError("Unknown source slot")
                result = sources[slot]
            return JSONResponse(result)

    async def decide(request):
        incoming = await body(request)
        with connect(root) as store:
            apply_decisions(store, incoming)
        return JSONResponse({"recorded": len(incoming)})

    async def scope_change(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) != {"id", "manifest_sha256", "mode"}
                or not all(isinstance(payload[key], str) for key in payload)):
            raise CrawlError("Scope changes require a candidate ID, manifest hash and scope mode")
        with connect(root) as store:
            store.db.execute("BEGIN IMMEDIATE")
            row = store.db.execute("SELECT * FROM candidates WHERE id=?", (payload["id"],)).fetchone()
            if not row or row["state"] not in ("approval_pending", "approved_waiting_batch", "deferred", "rejected"):
                raise CrawlError("Candidate cannot change scope in its current state")
            current = record(store, row)
            if current["manifest_sha256"] != payload["manifest_sha256"]:
                raise CrawlError("Candidate changed since review")
            scope = capture_scope(current["url"], payload["mode"])
            coverage = current["coverage"] or {}
            coverage["scope_mode"] = payload["mode"]
            store.db.execute("UPDATE candidates SET scope=?,coverage=?,decision=NULL,state='approval_pending' WHERE id=?", (scope, json.dumps(coverage), payload["id"]))
            store.db.commit()
            store.event(payload["id"], "scope_changed", {"scope": scope, "mode": payload["mode"]})
        return JSONResponse({"scope": scope})

    async def discovery(request):
        payload = await body(request)
        if (not isinstance(payload, dict) or set(payload) != {"max_candidates", "max_usd"}
                or type(payload["max_candidates"]) is not int or not 1 <= payload["max_candidates"] <= 50
                or type(payload["max_usd"]) not in (int, float) or not 0 < payload["max_usd"] <= 2):
            raise CrawlError("Discovery requires explicit bounds: 1–50 candidates and up to $2")
        with connect(root) as store:
            if store.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] >= 5000:
                raise CrawlError("Queue capacity reached; archive this queue before further discovery")
            operation = enqueue(store, "discover", payload)
        return JSONResponse({"operation": operation}, status_code=202)

    async def capture(request):
        payload = await body(request)
        ids = payload.get("ids") if isinstance(payload, dict) and set(payload) == {"ids"} else None
        if (not isinstance(ids, list) or not 1 <= len(ids) <= LIMITS["sites"]
                or not all(isinstance(value, str) for value in ids) or len(set(ids)) != len(ids)):
            raise CrawlError("Select 1–5 approved candidates for a capture batch")
        with connect(root) as store:
            store.db.execute("BEGIN IMMEDIATE")
            sites = []
            for candidate in ids:
                row = store.db.execute("SELECT * FROM candidates WHERE id=?", (candidate,)).fetchone()
                if not row or row["state"] != "approved_waiting_batch":
                    raise CrawlError("Capture requires a current site approval")
                site = record(store, row)
                if site["decision"]["manifest_sha256"] != site["manifest_sha256"]:
                    raise CrawlError("Site approval is stale")
                checked_sources(store, site)
                snapshots = [item for item in site["captures"] if
                             (original_url(item["url"]) == original_url(site["url"]) if site["scope_mode"] == "page"
                              else within_scope(item["url"], site["scope"]))]
                if not snapshots:
                    raise CrawlError("Approved scope excludes its reviewed source")
                sites.append({**{key: site[key] for key in ("id", "url", "scope", "scope_mode", "manifest_sha256", "decision")}, "captures": snapshots})
            batch_id = identifier()
            operation = enqueue(store, "capture", {"batch_id": batch_id, "sites": sites}, commit=False)
            store.db.execute("INSERT INTO batches VALUES (?,'capturing',NULL,NULL,NULL,NULL,NULL,?,?)", (batch_id, now(), now()))
            for candidate in ids:
                store.db.execute("UPDATE candidates SET state='capturing' WHERE id=?", (candidate,))
            store.db.commit()
        return JSONResponse({"operation": operation, "batch": batch_id}, status_code=202)

    async def publication(request):
        payload = await body(request)
        if not isinstance(payload, dict) or set(payload) not in ({"id", "manifest_sha256"}, {"id", "manifest_sha256", "slots"}):
            raise CrawlError("Publication requires the reviewed batch ID and manifest hash")
        with connect(root) as store:
            store.db.execute("BEGIN IMMEDIATE")
            row = store.db.execute("SELECT * FROM batches WHERE id=?", (valid_id(payload["id"]),)).fetchone()
            if not row:
                raise CrawlError("Unknown batch")
            batch = unpack(row)
            if batch["state"] != "awaiting_review" or batch["manifest_sha256"] != payload["manifest_sha256"]:
                raise CrawlError("Batch changed since review or is already approved")
            manifest = batch["manifest"]
            slots = payload.get("slots", list(range(len(manifest["captures"]))))
            if (not isinstance(slots, list) or not slots or not all(type(slot) is int and 0 <= slot < len(manifest["captures"]) for slot in slots)
                    or len(set(slots)) != len(slots)):
                raise CrawlError("Select at least one capture from the reviewed file set")
            if len(slots) != len(manifest["captures"]):
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
            if store.db.execute("SELECT 1 FROM operations WHERE state IN ('queued','running')").fetchone():
                raise CrawlError("Another operation is queued or running")
            result = store.db.execute("UPDATE operations SET state='queued',error=NULL,updated=? WHERE id=? AND state='interrupted'", (now(), valid_id(payload["id"])))
            if result.rowcount != 1:
                raise CrawlError("Operation is not awaiting an explicit resume")
            store.db.commit()
        return JSONResponse({"resumed": payload["id"]}, status_code=202)

    async def problem(request, error):
        return JSONResponse({"error": str(error)}, status_code=409)

    app = Starlette(routes=[Route("/", screen), Route("/assets/{name}", asset), Route("/healthz", health),
                            Route("/api/queue", listing), Route("/api/source", source),
                            Route("/api/decisions", decide, methods=["POST"]), Route("/api/scope", scope_change, methods=["POST"]), Route("/api/discover", discovery, methods=["POST"]),
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
