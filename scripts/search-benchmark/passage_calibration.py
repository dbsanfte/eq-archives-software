"""Frozen, read-only passage/window ablations against the actual search builders.

Collection uses one bounded PIT. Scoring is serial and resumable outside that PIT;
Luna judging and its shared $20 ledger remain a separate explicit command.
"""

import argparse
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import benchmark as b
import calibration as c

VARIANTS = [("current-r1", "legacy", 20, False)] + [
    (f"{mode}-{window}", mode, window, True)
    for mode in ("legacy", "semantic", "paired")
    for window in (20, 40, 60)
]


def permute(hits, scores, window):
    """Only supported slots within the selected head move; the tail is untouched."""
    result = list(hits)
    slots = [n for n, h in enumerate(hits[:window]) if h["_id"] in scores]
    ranked = sorted(slots, key=lambda n: (-scores[hits[n]["_id"]], n))
    for target, origin in zip(slots, ranked):
        result[target] = hits[origin]
    return result


def research_query(q, vector, model, filters, hybrid):
    from eqarchives_mcp.ranking_policy import hybrid_branches
    from eqarchives_mcp.research import has_constraints

    body = c.mcp_query(q, vector, model, filters)
    if hybrid and q.get("filters") and not has_constraints(q["text"]):
        body["knn"] = hybrid_branches(vector, model)
        for branch in body["knn"]:
            branch["filter"] = deepcopy(body["query"]["bool"]["filter"]) + (
                [branch["filter"]] if "filter" in branch else []
            )
    return body


def collect(args):
    from eqarchives_mcp.passages import evidence_query, evidence_rows
    from eqarchives_mcp.web_api import Builder

    root = Path(args.out)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    queries = b.load(args.queries)
    b.validate_suite(queries, [{"id": "bounded", "mode": "hybrid"}])
    if not 1 <= len(queries) <= 300:
        raise b.BenchmarkError("Require 1..300 queries")
    auth = b.load(args.credentials)
    es = b.Elasticsearch(
        args.es_url,
        {
            "Authorization": "Basic "
            + base64.b64encode(
                (auth["es_username"] + ":" + auth["es_password"]).encode()
            ).decode()
        },
        ca_file=args.es_ca_file,
    )
    embeddings = b.HTTP(
        args.embedding_url, {"Authorization": "Bearer " + auth["embedding_api_key"]}
    )
    cache = Path(args.embedding_cache)
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    frontend = b.Frontend(args.node_image)
    try:
        contract = frontend.call({"op": "contract"})
        model = contract["engine"]["embeddingModel"]
        vectors = {
            q["id"]: b.embedding(embeddings, model, q["text"], cache)[0]
            for q in queries
        }
        manifest = b.provenance(contract, es, "eq-archive")
        manifest.update(
            status="running",
            queries=queries,
            configs=[
                dict(id=n, passages=m, window=w, filtered_hybrid=h)
                for n, m, w, h in VARIANTS
            ],
            depth=60,
            started_at=time.time(),
            surfaces=["portal_grouped", "mcp_raw"],
            cohort="entire live index",
            selection="tune only; validation judgments withheld until selection frozen",
        )
        b.save(root / "manifest.json", manifest)
        runs = []
        evidence = {}
        pooled = {q["id"]: {} for q in queries}
        with b.Snapshot(es, "eq-archive", 900) as snapshot:
            stats = snapshot.search(
                {
                    "size": 0,
                    "track_total_hits": True,
                    "query": {"match_all": {}},
                    "aggs": {
                        "versions": {
                            "terms": {
                                "field": "text_chunking_version",
                                "size": 10,
                                "missing": "none",
                            }
                        }
                    },
                }
            )
            manifest.update(
                cohort_count=stats["hits"]["total"],
                cohort_aggregations=stats["aggregations"],
            )
            for q in queries:
                vector = vectors[q["id"]]
                exported = frontend.call({"op": "build", "query": q, "vector": vector})
                portal = Builder().search(exported["body"], model)
                portal["_source"] = b.METADATA
                filters = portal.get("post_filter") or portal.get("query", {}).get(
                    "bool", {}
                ).get("filter", [])
                if "knn" in portal:
                    portal = c.configure(
                        portal,
                        {
                            "vector_fields": {"text.vector": 1},
                            "k": 100,
                            "num_candidates": 500,
                            "boost": 10,
                        },
                    )
                    data = c.fused(
                        snapshot,
                        frontend,
                        portal,
                        q,
                        {"rank_window_size": 100, "rrf_constant": 60},
                    )
                    hits = data["grouped"][:60]
                    # Match the portal's fixed RRF head followed by unseen lexical
                    # groups, never increase its fusion window in this experiment.
                    if len(hits) < 60:
                        lexical = deepcopy(portal)
                        lexical.pop("knn")
                        tail = b.retrieve(snapshot, frontend, lexical, q, 60, 0)[
                            "grouped"
                        ]
                        seen = {h["capture_key"] for h in hits}
                        hits += [h for h in tail if h["capture_key"] not in seen][
                            : 60 - len(hits)
                        ]
                else:
                    hits = b.retrieve(snapshot, frontend, portal, q, 60, 0)["grouped"][
                        :60
                    ]
                rows = [
                    dict(
                        query_id=q["id"],
                        surface="portal_grouped",
                        retrieval="current",
                        raw=hits,
                    )
                ]
                for hybrid in (False, True):
                    body = research_query(q, vector, model, filters, hybrid)
                    response = snapshot.search(
                        {
                            **body,
                            "_source": b.METADATA,
                            "size": 60,
                            "from": 0,
                            "track_total_hits": False,
                        }
                    )
                    raw = response["hits"]["hits"]
                    for h, key in zip(raw, frontend.call({"op": "keys", "hits": raw})):
                        h["capture_key"] = key
                        if not b.filter_matches(h["_source"], q.get("filters", [])):
                            raise b.BenchmarkError("MCP filter escaped")
                    rows.append(
                        dict(
                            query_id=q["id"],
                            surface="mcp_raw",
                            retrieval="hybrid" if hybrid else "current",
                            raw=raw,
                        )
                    )
                    if not q.get("filters"):
                        rows.append({**rows[-1], "retrieval": "hybrid"})
                        break
                ids = list(dict.fromkeys(h["_id"] for r in rows for h in r["raw"]))
                selected = {}
                for n in range(0, len(ids), 60):
                    # Only actual hybrid retrieval paths have a cached embedding.
                    from eqarchives_mcp.research import has_constraints

                    response = snapshot.search(
                        evidence_query(
                            q["text"],
                            ids[n : n + 60],
                            model,
                            None if has_constraints(q["text"]) else vector,
                            include_fallback=True,
                        )
                    )
                    selected.update(
                        {h["_id"]: evidence_rows(h) for h in response["hits"]["hits"]}
                    )
                if set(selected) != set(ids):
                    raise b.BenchmarkError("Missing nested evidence")
                evidence[q["id"]] = selected
                for row in rows:
                    runs.append(row)
                    pooled[q["id"]].update({h["_id"]: h for h in row["raw"]})
                print(q["id"], "retrieved", len(ids), "documents", flush=True)
            sources = {}
            ids = sorted({id for p in pooled.values() for id in p})
            for n in range(0, len(ids), 10):
                response = snapshot.search(
                    {
                        "size": 10,
                        "_source": b.METADATA
                        + b.PASSAGE_FIELDS
                        + ["text_full", "llm_image_text_full"],
                        "query": {"ids": {"values": ids[n : n + 10]}},
                    }
                )
                for h in response["hits"]["hits"]:
                    sources[h["_id"]] = {
                        "source": h["_source"],
                        "source_sha256": b.digest(h["_source"]),
                    }
            if set(sources) != set(ids):
                raise b.BenchmarkError("Missing complete source evidence")
        b.save(root / "sources.json", sources)
        b.save(root / "passages.json", evidence)
        b.save(root / "pool.json", {q: list(p.values()) for q, p in pooled.items()})
        (root / "runs.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in runs)
        )
        manifest.update(
            status="complete",
            completed_at=time.time(),
            pooled_documents=len(sources),
            passages_sha256=b.digest(evidence),
        )
        b.save(root / "manifest.json", manifest)
    finally:
        frontend.close()


def rank(args):
    import urllib.request
    from tokenizers import Tokenizer
    from eqarchives_mcp.passages import select_evidence
    from eqarchives_mcp.rerank import passage, scores, MODEL, TOKENIZER_SHA

    root = Path(args.run)
    manifest = b.load(root / "manifest.json")
    if manifest["status"] != "complete":
        raise b.BenchmarkError("Incomplete retrieval")
    evidence = b.load(root / "passages.json")
    sources = b.load(root / "sources.json")
    if b.digest(evidence) != manifest["passages_sha256"] or any(
        b.digest(s["source"]) != s["source_sha256"] for s in sources.values()
    ):
        raise b.BenchmarkError("Changed evidence")
    if hashlib.sha256(Path(args.tokenizer).read_bytes()).hexdigest() != TOKENIZER_SHA:
        raise b.BenchmarkError("Tokenizer differs")
    tokenizer = Tokenizer.from_file(args.tokenizer)
    key = Path(args.key).read_text().strip()
    scorepath = root / "qwen-scores.json"
    saved = b.load(scorepath) if scorepath.exists() else {}
    if any(type(v) not in (float, int) or not 0 <= v <= 1 for v in saved.values()):
        raise b.BenchmarkError("Invalid cached score")
    client = b.HTTP(args.url, {"Authorization": "Bearer " + key})
    queries = {q["id"]: q for q in manifest["queries"]}
    runs = b.jsonlines(root / "runs.jsonl")
    out = []
    for qid, q in queries.items():
        prepared = {}
        inputs = {}
        for id, source in evidence[qid].items():
            raw = sources[id]["source"]
            for mode in ("legacy", "semantic", "paired"):
                docs = (
                    [passage(q["text"], raw, tokenizer)]
                    if mode == "legacy"
                    else [
                        passage(
                            q["text"],
                            {"title": source["title"], "text": [r]},
                            tokenizer,
                        )
                        for r in select_evidence(source, mode)
                    ]
                )
                if not docs:
                    docs = [passage(q["text"], raw, tokenizer)]
                signatures = []
                for d in docs:
                    if d:
                        sig = b.digest([q["text"], d, MODEL])
                        inputs[sig] = d
                        signatures.append(sig)
                prepared[id, mode] = signatures
        missing = [(s, d) for s, d in inputs.items() if s not in saved]
        start = time.monotonic()
        while missing:
            batch = missing[:40]
            while (
                len(
                    json.dumps(
                        {
                            "model": MODEL,
                            "query": q["text"],
                            "documents": [d for _, d in batch],
                        }
                    ).encode()
                )
                > 128 * 1024
            ):
                if len(batch) < 2:
                    raise b.BenchmarkError("Prompt byte bound")
                batch = batch[: len(batch) // 2]
            response = client.request(
                "POST",
                "/v1/rerank",
                {
                    "model": MODEL,
                    "query": q["text"],
                    "documents": [d for _, d in batch],
                },
            )
            saved.update(
                {s: v for (s, _), v in zip(batch, scores(response, len(batch)))}
            )
            b.save(scorepath, saved)
            missing = missing[len(batch) :]
        for surface in ("portal_grouped", "mcp_raw"):
            for name, mode, window, hybrid in VARIANTS:
                retrieval = "hybrid" if hybrid and surface == "mcp_raw" else "current"
                row = next(
                    r
                    for r in runs
                    if r["query_id"] == qid
                    and r["surface"] == surface
                    and r["retrieval"] == retrieval
                )
                values = {
                    h["_id"]: max(saved[s] for s in prepared[h["_id"], mode])
                    for h in row["raw"]
                    if prepared[h["_id"], mode]
                }
                ranking = permute(row["raw"], values, window)
                out.append(
                    {**row, "config_id": name, "raw": ranking, "grouped": ranking}
                )
        print(qid, "scored", round(time.monotonic() - start, 2), "seconds", flush=True)
    (root / "ranked.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out)
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    cparser = sub.add_parser("collect")
    for arg in (
        "out",
        "queries",
        "credentials",
        "es-url",
        "es-ca-file",
        "embedding-url",
        "embedding-cache",
    ):
        cparser.add_argument("--" + arg, required=True)
    cparser.add_argument("--node-image", default="eqarchives-benchmark-node")
    rparser = sub.add_parser("rank")
    for arg in ("run", "tokenizer", "key"):
        rparser.add_argument("--" + arg, required=True)
    rparser.add_argument("--url", default="http://10.42.0.1:8083")
    args = p.parse_args()
    collect(args) if args.command == "collect" else rank(args)
