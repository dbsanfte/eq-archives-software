"""Explicit Luna-only blinded judging with a durable shared dollar reservation cap."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import sqlite3
import urllib.request

import benchmark as b
from grade import MODEL, PROMPT, SCHEMA, response_ratings


class Ledger:
    def __init__(self, path, cap):
        if not math.isfinite(cap) or cap <= 0:
            raise b.BenchmarkError("A finite positive dollar cap is required")
        self.path, self.cap = str(path), cap
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, reserved REAL NOT NULL, actual REAL, response TEXT, status TEXT NOT NULL)"
            )

    def connection(self):
        return sqlite3.connect(self.path, timeout=30)

    def reserve(self, identity, payload):
        # UTF-8 bytes conservatively bound token count; use long-context rates
        # even though ordinary batches are far below the 272k input threshold.
        maximum = (
            len(json.dumps(payload, ensure_ascii=False).encode()) + 8192
        ) * 0.20 / 1e6 + payload["max_output_tokens"] * 0.75 / 1e6
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT response FROM attempts WHERE id=?", (identity,)
            ).fetchone()
            if old:
                if old[0]:
                    return json.loads(old[0])
                raise b.BenchmarkError(
                    "Unsettled prior attempt retained; do not silently retry"
                )
            spent = db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM attempts"
            ).fetchone()[0]
            if spent + maximum > self.cap:
                raise b.BenchmarkError("Luna dollar budget reached")
            db.execute(
                "INSERT INTO attempts VALUES (?,?,NULL,NULL,?)",
                (identity, maximum, "reserved"),
            )
        return None

    def settle(self, identity, response):
        usage = response.get("usage", {})
        input_tokens = usage.get("input_tokens")
        output = usage.get("output_tokens")
        cached = usage.get("input_tokens_details", {}).get("cached_tokens", 0)
        if (
            any(type(n) is not int or n < 0 for n in (input_tokens, output, cached))
            or cached > input_tokens
        ):
            raise b.BenchmarkError("Usage missing; reservation remains charged")
        multiplier = 2 if input_tokens > 272000 else 1
        actual = (
            (input_tokens - cached) * 0.1 * multiplier
            + cached * 0.01 * multiplier
            + output * (0.75 if multiplier == 2 else 0.5)
        ) / 1e6
        with self.connection() as db:
            db.execute(
                "UPDATE attempts SET actual=?,response=?,status=? WHERE id=?",
                (actual, json.dumps(response), "settled", identity),
            )

    def summary(self):
        with self.connection() as db:
            rows = db.execute(
                "SELECT reserved,actual,response FROM attempts"
            ).fetchall()
        return {
            "cap_usd": self.cap,
            "requests": len(rows),
            "estimated_usd": sum(a or 0 for _, a, _ in rows),
            "uncertain_reserved_usd": sum(r for r, a, _ in rows if a is None),
            "pricing_reference": "https://developers.openai.com/api/docs/models/gpt-6-luna",
            "input_tokens": sum(
                json.loads(s).get("usage", {}).get("input_tokens", 0)
                for _, _, s in rows
                if s
            ),
            "output_tokens": sum(
                json.loads(s).get("usage", {}).get("output_tokens", 0)
                for _, _, s in rows
                if s
            ),
        }


def reusable_ratings(paths, queries, sources, signature):
    """Reuse only the same judge, complete source and query, across snapshots."""
    reused = {}
    for path in paths:
        for row in b.jsonlines(path):
            qid, doc = row["query_id"], row["doc_id"]
            if qid not in queries or doc not in sources:
                continue
            if (
                row.get("grader_signature") == signature
                and row.get("source_sha256") == sources[doc]["source_sha256"]
                and row.get("query_sha256") == b.digest(queries[qid])
                and row.get("model") == MODEL
                and row.get("origin") == "model"
                and type(row.get("grade")) is int
                and 0 <= row["grade"] <= 3
            ):
                reused[qid, doc] = row
    return reused


def grade(args):
    root = Path(args.run)
    manifest = b.load(root / "manifest.json")
    if args.split == "validation" and not (root / "selection.json").exists():
        raise b.BenchmarkError("Freeze tuning selection before validation judging")
    if args.split == "tune" and (root / "selection.json").exists():
        raise b.BenchmarkError("Tuning evidence is already frozen")
    sources = b.load(root / "sources.json")
    runs = b.jsonlines(root / "ranked.jsonl")
    if manifest["status"] != "complete":
        raise b.BenchmarkError("Retrieval incomplete")
    signature = b.digest(
        {
            "prompt": PROMPT,
            "model": MODEL,
            "schema": SCHEMA,
            "version": "calibration-blind-v1",
        }
    )
    ledger = Ledger(root.parent / "luna-ledger.sqlite3", args.max_usd)
    key = Path(args.key).read_text().strip()
    selected = {q["id"]: set() for q in manifest["queries"] if q["split"] == args.split}
    for r in runs:
        if r["query_id"] in selected:
            selected[r["query_id"]].update(h["_id"] for h in r["raw"][:10])
    reused = reusable_ratings(
        args.reuse_ratings,
        {q["id"]: q for q in manifest["queries"] if q["id"] in selected},
        sources,
        signature,
    )
    tasks = []
    skipped = []
    for q in manifest["queries"]:
        if q["id"] not in selected:
            continue
        ids = sorted(selected[q["id"]])
        random.Random(b.digest([q["id"], signature])).shuffle(ids)
        documents = []
        for id in ids:
            src = sources[id]
            if b.digest(src["source"]) != src["source_sha256"]:
                raise b.BenchmarkError("Changed source evidence")
            if (q["id"], id) in reused:
                continue
            text = (
                src["source"].get("text_full")
                or src["source"].get("llm_image_text_full")
                or ""
            )
            if len(text.encode()) > 900000:
                skipped.append(
                    {
                        "query_id": q["id"],
                        "doc_id": id,
                        "reason": "Complete source exceeds bounded judging request; unjudged",
                    }
                )
                continue
            documents.append((id, text))
        batch = []
        size = 0
        for doc in documents:
            if batch and (len(batch) >= 6 or size + len(doc[1]) > 100000):
                tasks.append((q, batch))
                batch = []
                size = 0
            batch.append(doc)
            size += len(doc[1])
        if batch:
            tasks.append((q, batch))

    def judge(task):
        q, docs = task
        evidence = {
            "query": q["text"],
            "information_need": q["intent"],
            "filters": q.get("filters", []),
            "documents": [
                {
                    "slot": n,
                    "complete_original_source": text,
                    "title": sources[id]["source"].get("title", ""),
                }
                for n, (id, text) in enumerate(docs)
            ],
        }
        payload = {
            "model": MODEL,
            "store": False,
            "service_tier": "default",
            "instructions": PROMPT,
            "input": json.dumps(evidence, ensure_ascii=False),
            "reasoning": {"effort": "low"},
            "max_output_tokens": 32768,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "relevance_grades",
                    "strict": True,
                    "schema": SCHEMA,
                }
            },
        }
        identity = b.digest(
            [signature, q, [(id, sources[id]["source_sha256"]) for id, _ in docs]]
        )
        response = ledger.reserve(identity, payload)
        if response is None:

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None

            request = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + key,
                },
            )
            try:
                with urllib.request.build_opener(NoRedirect).open(
                    request, timeout=120
                ) as r:
                    raw = r.read(2 * 1024 * 1024 + 1)
                    if len(raw) > 2 * 1024 * 1024:
                        raise b.BenchmarkError("Oversized judgment response")
                    response = json.loads(raw)
            except Exception:
                raise b.BenchmarkError(
                    "Luna request failed; its reservation is retained, credentials/upstream body omitted"
                ) from None
            ledger.settle(identity, response)
        if response.get("model") != MODEL or response.get("service_tier") != "default":
            raise b.BenchmarkError("Unexpected judging model or service tier")
        ratings = response_ratings(response, len(docs))
        result = []
        for row in ratings:
            id = docs[row["slot"]][0]
            result.append(
                {k: v for k, v in row.items() if k != "slot"}
                | {
                    "query_id": q["id"],
                    "doc_id": id,
                    "origin": "model",
                    "model": MODEL,
                    "grader_signature": signature,
                    "source_sha256": sources[id]["source_sha256"],
                    "query_sha256": b.digest(q),
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        return result

    output = root / f"ratings-{args.split}.jsonl"
    # Rewrite from durable per-attempt receipts; a resumed run never repeats paid work.
    with output.open("w") as stream, ThreadPoolExecutor(max_workers=3) as executor:
        for (qid, doc), row in reused.items():
            if doc in selected[qid]:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
        for n, rows in enumerate(executor.map(judge, tasks), 1):
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            print(
                args.split,
                n,
                "/",
                len(tasks),
                ledger.summary()["estimated_usd"],
                flush=True,
            )
    b.save(root / f"skipped-{args.split}.json", skipped)
    b.save(root / "luna-usage.json", ledger.summary())
    print(json.dumps(ledger.summary()), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    for name in ("run", "key"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--split", choices=["tune", "validation"], required=True)
    p.add_argument("--max-usd", type=float, default=20)
    p.add_argument("--reuse-ratings", type=Path, action="append", default=[])
    grade(p.parse_args())
