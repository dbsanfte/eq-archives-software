"""Private, bounded OpenAI embeddings API and Qwen/llama.cpp rerank API."""

import asyncio
import base64
import hmac
import json
import math
import os
from pathlib import Path
import struct
import time

from backend import EMBED_MODEL, RERANK_MODEL, PREFIX, SUFFIX
from scheduler import Busy, Scheduler


def text(value, limit=8192):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ValueError("Expected nonempty bounded text")
    return value


def validate(kind, body):
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object")
    model = EMBED_MODEL if kind == "embed" else RERANK_MODEL
    allowed = (
        {"model", "input", "encoding_format", "dimensions", "user"}
        if kind == "embed"
        else {"model", "query", "documents"}
    )
    if body.keys() - allowed or body.get("model") != model:
        raise ValueError("Unsupported model or request fields")
    if kind == "rerank":
        query, docs = text(body.get("query"), 2048), body.get("documents")
        if not isinstance(docs, list) or not 1 <= len(docs) <= 120:
            raise ValueError("Expected 1–120 documents")
        return [PREFIX + query + "\n<Document>: " + text(d) + SUFFIX for d in docs], False
    if body.get("encoding_format", "float") not in ("float", "base64"):
        raise ValueError("encoding_format must be float or base64")
    if "dimensions" in body and (type(body["dimensions"]) is not int or body["dimensions"] != 768):
        raise ValueError("This model supports 768 dimensions")
    if "user" in body:
        text(body["user"], 256)
    rows = body.get("input")
    if isinstance(rows, str):
        rows = [rows]
    if isinstance(rows, list) and rows and all(type(x) is int for x in rows):
        rows = [rows]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError("Expected 1–64 embedding inputs")
    token_input = all(isinstance(row, list) for row in rows)
    if token_input:
        for row in rows:
            if not 1 <= len(row) <= 512 or any(type(x) is not int or not 0 <= x < 262144 for x in row):
                raise ValueError("Invalid token input")
    else:
        for row in rows:
            text(row, 16384)
    return rows, token_input


def response(kind, values, tokens, body):
    model = EMBED_MODEL if kind == "embed" else RERANK_MODEL
    if len(values) != len(tokens):
        raise RuntimeError("Incomplete inference response")
    count = sum(map(len, tokens))
    usage = {"prompt_tokens": count, "total_tokens": count}
    if kind == "rerank":
        if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
            raise RuntimeError("Invalid relevance scores")
        return {
            "model": model,
            "results": [{"index": i, "relevance_score": v} for i, v in enumerate(values)],
            "usage": usage,
        }
    data = []
    for i, row in enumerate(values):
        if (
            len(row) != 768
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in row)
            or not any(row)
        ):
            raise RuntimeError("Invalid embedding")
        vector = (
            base64.b64encode(struct.pack("<768f", *row)).decode()
            if body.get("encoding_format") == "base64"
            else row
        )
        data.append({"object": "embedding", "index": i, "embedding": vector})
    return {"object": "list", "data": data, "model": model, "usage": usage}


class App:
    def __init__(self, engine=None, keys=None, batch=16):
        self.engine, self.keys, self.batch = engine, keys, batch
        self.scheduler = None
        self.active = 0
        self.active_by_kind = {"embed": 0, "rerank": 0}
        self.ready = False

    async def start(self):
        if type(self.batch) is not int or not 1 <= self.batch <= 32:
            raise ValueError("GPU batch size must be 1–32")
        if self.engine is None:
            from scheduler import Engine

            self.keys = {
                k: Path("/run/secrets/" + k + "_key").read_text().strip().encode()
                for k in ("embed", "rerank")
            }
        # Reuse operator-managed endpoint credentials without rotating credentials
        # underneath existing ingestion jobs. No default or generated public key.
        if not self.keys or set(self.keys) != {"embed", "rerank"} or any(not v for v in self.keys.values()):
            raise ValueError("Private service keys are required")
        if self.engine is None:
            self.engine = await asyncio.to_thread(Engine, os.environ.get("MODEL_ROOT", "/models"))
        self.scheduler = Scheduler(self.engine, batch=self.batch)
        self.scheduler.start()
        self.ready = True

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    try:
                        await self.start()
                        await send({"type": "lifespan.startup.complete"})
                    except Exception:
                        await send(
                            {
                                "type": "lifespan.startup.failed",
                                "message": "CUDA service initialization failed",
                            }
                        )
                        return
                else:
                    self.ready = False
                    if self.scheduler:
                        await self.scheduler.close()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path, method = scope["path"], scope["method"]
        if path == "/health" and method == "GET":
            healthy = self.ready and self.scheduler.healthy()
            await self.send(
                send,
                200 if healthy else 503,
                {
                    "status": "ok" if healthy else "unavailable",
                    "revision": os.getenv("GIT_SHA", "development"),
                },
            )
            return
        kind = {"/v1/embeddings": "embed", "/v1/rerank": "rerank"}.get(path)
        if not kind or method != "POST" or scope.get("query_string"):
            await self.send(send, 404, {"error": {"message": "Unknown route"}})
            return
        headers = scope["headers"]
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        if not self.keys or len(auth) != 1 or not hmac.compare_digest(auth[0], b"Bearer " + self.keys[kind]):
            await self.send(send, 401, {"error": {"message": "Invalid credential"}})
            return
        # Reserve eight full-lifecycle slots for each model, including uploads
        # and slow response readers. Bulk clients cannot consume rerank admission.
        if not self.ready or not self.scheduler.healthy() or self.active_by_kind[kind] >= 8:
            await self.send(send, 503, {"error": {"message": "Inference capacity unavailable"}})
            return
        self.active += 1
        self.active_by_kind[kind] += 1
        try:
            await asyncio.wait_for(self.request(kind, receive, send), 15)
        except asyncio.TimeoutError:
            # If delivery itself timed out, terminate the connection rather than
            # attempt a second response after headers/body may already be sent.
            raise
        finally:
            self.active -= 1
            self.active_by_kind[kind] -= 1

    async def request(self, kind, receive, send):
        try:
            raw = bytearray()
            deadline = time.monotonic() + 2
            while True:
                event = await asyncio.wait_for(receive(), max(0, deadline - time.monotonic()))
                if event["type"] == "http.disconnect":
                    return
                raw.extend(event.get("body", b""))
                if len(raw) > 256 * 1024:
                    await self.send(send, 413, {"error": {"message": "Request exceeds 256 KiB"}})
                    return
                if not event.get("more_body"):
                    break

            def unique(pairs):
                d = {}
                for k, v in pairs:
                    if k in d:
                        raise ValueError("Duplicate JSON field")
                    d[k] = v
                return d

            body = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
            )
            rows, token_input = validate(kind, body)
            tokens = rows if token_input else await asyncio.to_thread(self.engine.encode, kind, rows)
            inference = asyncio.create_task(self.scheduler.submit(kind, tokens, time.monotonic() + 10))
            disconnect = asyncio.create_task(receive())
            try:
                done, _ = await asyncio.wait([inference, disconnect], return_when=asyncio.FIRST_COMPLETED)
                if disconnect in done:
                    return
                values = await inference
            finally:
                for task in (inference, disconnect):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(inference, disconnect, return_exceptions=True)
            result = response(kind, values, tokens, body)
            code = 200
        except (ValueError, TypeError, UnicodeError, RecursionError):
            code, result = 400, {
                "error": {"message": "Invalid input, unsupported field, or token limit exceeded"}
            }
        except asyncio.TimeoutError:
            code, result = 504, {"error": {"message": "Inference deadline exceeded"}}
        except Busy:
            code, result = 503, {"error": {"message": "Inference queue is full"}}
        except Exception:
            code, result = 503, {"error": {"message": "GPU inference unavailable"}}
        await self.send(send, code, result)

    @staticmethod
    async def send(send, code, body):
        data = json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        if len(data) > 2 * 1024 * 1024:
            raise RuntimeError("Output bound exceeded")
        await send(
            {
                "type": "http.response.start",
                "status": code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(data)).encode()),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ],
            }
        )
        for i in range(0, len(data), 65536):
            await send({"type": "http.response.body", "body": data[i : i + 65536], "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        App(batch=int(os.environ.get("GPU_BATCH_SIZE", "16"))),
        host="0.0.0.0",
        port=8080,
        workers=1,
        access_log=False,
        server_header=False,
        timeout_keep_alive=5,
        limit_concurrency=32,
        h11_max_incomplete_event_size=16384,
    )
