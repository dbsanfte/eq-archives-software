"""Bounded full-index portal/MCP calibration with frozen source evidence."""

import argparse
import base64
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import random
import time

import benchmark as b


def configure(body, config):
    """Explicit operator experiment, applied after production query construction."""
    result = deepcopy(body)
    weights = config.get("lexical_fields")

    def change(node):
        if isinstance(node, dict):
            for kind, value in node.items():
                if kind in ("multi_match", "simple_query_string"):
                    if weights:
                        value["fields"] = [
                            f"{name}^{weight}" for name, weight in weights.items()
                        ]
                    # Never reinterpret explicit phrase/boolean constraints.
                    if config.get("minimum_should_match") and "knn" in result:
                        value["minimum_should_match"] = config["minimum_should_match"]
                else:
                    change(value)
        elif isinstance(node, list):
            for value in node:
                change(value)

    change(result.get("query", {}))
    if "knn" in result:
        fields = config.get("vector_fields")
        result["knn"] = [
            row for row in result["knn"] if fields is None or row["field"] in fields
        ]
        for row in result["knn"]:
            row.update(
                k=config.get("k", row.get("k", 50)),
                num_candidates=config.get(
                    "num_candidates", row.get("num_candidates", 250)
                ),
                boost=config.get("boost", row.get("boost", 10))
                * (fields[row["field"]] if fields else 1),
            )
            if "similarity" in config:
                row["similarity"] = config["similarity"]
    return result


def fused(snapshot, frontend, body, query, config):
    """Read-only application RRF: fixed raw window, grouping after fusion."""
    window = config["rank_window_size"]
    if type(window) is not int or not 20 <= window <= 1000:
        raise b.BenchmarkError("RRF window must be between 20 and 1000")
    lexical = deepcopy(body)
    lexical.pop("knn")
    semantic = deepcopy(body)
    semantic.pop("query", None)
    rankings = []
    took = 0
    start = time.monotonic()
    for branch in (lexical, semantic):
        response = snapshot.search(
            {**branch, "size": window, "from": 0, "track_total_hits": False}
        )
        hits = response["hits"]["hits"]
        keys = frontend.call({"op": "keys", "hits": hits})
        for hit, key in zip(hits, keys):
            hit["capture_key"] = key
            if not b.filter_matches(hit["_source"], query.get("filters", [])):
                raise b.BenchmarkError("RRF filter escaped")
        rankings.append(hits)
        took += response.get("took", 0)
    raw = b.rrf(*rankings, config["rrf_constant"])
    return {
        "raw": raw,
        "grouped": b.grouped(raw),
        "request_ms": (time.monotonic() - start) * 1000,
        "es_ms": took,
        "window_reached": any(len(rows) == window for rows in rankings),
    }


def blended(hits, values, alpha):
    """Blend reciprocal positions inside the same top-20; unsupported slots stay."""
    head = hits[:20]
    slots = [n for n, h in enumerate(head) if h["_id"] in values]
    sorted_slots = sorted(slots, key=lambda n: (-values[head[n]["_id"]], n))
    positions = {n: rank for rank, n in enumerate(sorted_slots)}
    sorted_slots.sort(
        key=lambda n: (
            -(alpha / (10 + positions[n] + 1) + (1 - alpha) / (10 + n + 1)),
            n,
        )
    )
    result = list(hits)
    for target, origin in zip(slots, sorted_slots):
        result[target] = head[origin]
    return result


def mcp_query(q, vector, model, filters):
    from eqarchives_mcp.ranking_policy import hybrid_branches
    from eqarchives_mcp.research import keyword_query, has_constraints, research_sort

    mcp = {
        "query": keyword_query(q["text"]),
        "_source": b.METADATA,
    }
    # search_archive is intentionally keyword-only for filtered research.
    if q.get("filters"):
        mcp["sort"] = research_sort("relevance")
        mcp["query"] = {
            "bool": {
                "must": [mcp["query"]],
                "filter": (filters if isinstance(filters, list) else [filters]),
            }
        }
    elif not has_constraints(q["text"]) and len(q["text"]) <= 600:
        mcp["knn"] = hybrid_branches(vector, model)
    return mcp


def collect(args):
    from eqarchives_mcp.web_api import Builder

    root = Path(args.out)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    queries = b.load(args.queries)
    configs = b.load(args.configs)
    b.validate_suite(queries, configs)
    if len(queries) * len(configs) * 2 > 1400:
        raise b.BenchmarkError("Case budget exceeded")
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
    frontend = b.Frontend(args.node_image)
    try:
        contract = frontend.call({"op": "contract"})
        model = contract["engine"]["embeddingModel"]
        cache = (
            Path(args.embedding_cache)
            if getattr(args, "embedding_cache", None)
            else root / "embedding-cache"
        )
        cache.mkdir(mode=0o700, exist_ok=True)
        vectors = {
            q["id"]: b.embedding(embeddings, model, q["text"], cache)[0]
            for q in queries
        }
        manifest = b.provenance(contract, es, "eq-archive")
        manifest.update(
            status="running",
            queries=queries,
            configs=configs,
            depth=20,
            archive_git_sha=args.archive_sha,
            started_at=time.time(),
            surfaces=["portal_grouped", "mcp_raw"],
            cohort="entire live index",
            selection="tune only; validation judgments withheld until selection frozen",
        )
        b.save(root / "manifest.json", manifest)
        runs = []
        pool = {q["id"]: {} for q in queries}
        requests = []
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
                        },
                        "domains": {"terms": {"field": "domain_name", "size": 20}},
                        "lists": {"terms": {"field": "mailing_list_name", "size": 20}},
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
                mcp = mcp_query(q, vector, model, filters)
                order = list(configs)
                random.Random(q["id"]).shuffle(order)
                for c in order:
                    for surface, base in [("portal_grouped", portal), ("mcp_raw", mcp)]:
                        effective = (
                            {**c, **contract.get("portal_retrieval", {})}
                            if c["id"] == "current" and surface == "portal_grouped"
                            else c
                        )
                        body = configure(base, effective)
                        if effective["mode"] == "rrf" and body.get("knn"):
                            data = fused(snapshot, frontend, body, q, effective)
                        elif surface == "portal_grouped":
                            data = b.retrieve(snapshot, frontend, body, q, 20, 0.03)
                        else:
                            start = time.monotonic()
                            response = snapshot.search(
                                {**body, "size": 20, "track_total_hits": False}
                            )
                            hits = response["hits"]["hits"]
                            keys = frontend.call({"op": "keys", "hits": hits})
                            for h, key in zip(hits, keys):
                                h["capture_key"] = key
                                if not b.filter_matches(
                                    h["_source"], q.get("filters", [])
                                ):
                                    raise b.BenchmarkError("MCP filter escaped")
                            data = {
                                "raw": hits,
                                "grouped": b.grouped(hits),
                                "request_ms": (time.monotonic() - start) * 1000,
                                "es_ms": response.get("took", 0),
                                "window_reached": False,
                            }
                        data.update(
                            query_id=q["id"], config_id=c["id"], surface=surface
                        )
                        runs.append(data)
                        requests.append(
                            {
                                "query_id": q["id"],
                                "config_id": c["id"],
                                "surface": surface,
                                "body": body,
                                "effective_config": effective,
                            }
                        )
                        for h in (
                            data["grouped"]
                            if surface == "portal_grouped"
                            else data["raw"]
                        )[:20]:
                            pool[q["id"]][h["_id"]] = h
                print(q["id"], "retrieved", flush=True)
            sources = {}
            ids = sorted({id for hits in pool.values() for id in hits})
            for start in range(0, len(ids), 15):
                response = snapshot.search(
                    {
                        "size": 15,
                        "_source": b.METADATA
                        + b.PASSAGE_FIELDS
                        + ["text_full", "llm_image_text_full"],
                        "query": {"ids": {"values": ids[start : start + 15]}},
                    }
                )
                for h in response["hits"]["hits"]:
                    sources[h["_id"]] = {
                        "source": h["_source"],
                        "source_sha256": b.digest(h["_source"]),
                    }
            if len(sources) != len(ids):
                raise b.BenchmarkError("Missing source evidence")
            b.save(root / "sources.json", sources)
            b.save(
                root / "pool.json",
                {qid: list(hits.values()) for qid, hits in pool.items()},
            )
            b.save(root / "requests.json", requests)
            (root / "runs.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in runs)
            )
        manifest.update(
            status="complete", completed_at=time.time(), pooled_documents=len(sources)
        )
        b.save(root / "manifest.json", manifest)
    finally:
        frontend.close()


def rank(args):
    import urllib.request
    from tokenizers import Tokenizer
    from eqarchives_mcp.rerank import passage, scores, MODEL, TOKENIZER_SHA

    root = Path(args.run)
    sources = b.load(root / "sources.json")
    manifest = b.load(root / "manifest.json")
    if manifest["status"] != "complete":
        raise b.BenchmarkError("Retrieval incomplete")
    queries = {q["id"]: q for q in manifest["queries"]}
    for src in sources.values():
        if b.digest(src["source"]) != src["source_sha256"]:
            raise b.BenchmarkError("Source changed")
    if hashlib.sha256(Path(args.tokenizer).read_bytes()).hexdigest() != TOKENIZER_SHA:
        raise b.BenchmarkError("Reranker tokenizer differs from the production pin")
    tokenizer = Tokenizer.from_file(args.tokenizer)
    key = Path(args.key).read_text().strip()
    scorefile = root / "qwen-scores.json"
    saved = b.load(scorefile) if scorefile.exists() else {}
    if any(
        type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
        for v in saved.values()
    ):
        raise b.BenchmarkError("Invalid cached score")
    out = []
    prepared = {}

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    client = urllib.request.build_opener(NoRedirect)
    for row in b.jsonlines(root / "runs.jsonl"):
        hits = (row["grouped"] if row["surface"] == "portal_grouped" else row["raw"])[
            :20
        ]
        qid = row["query_id"]
        query = queries[qid]["text"]
        inputs = []
        values = {}
        for h in hits:
            src = sources[h["_id"]]
            pair = (qid, h["_id"])
            if pair not in prepared:
                prepared[pair] = passage(query, src["source"], tokenizer)
            doc = prepared[pair]
            if doc:
                signature = b.digest([query, doc, MODEL])
                inputs.append((h["_id"], signature, doc))
        missing = list(
            {
                signature: doc for _, signature, doc in inputs if signature not in saved
            }.items()
        )
        elapsed = 0
        if missing:
            start = time.monotonic()
            request = urllib.request.Request(
                args.url.rstrip("/") + "/v1/rerank",
                data=json.dumps(
                    {
                        "model": MODEL,
                        "query": query,
                        "documents": [d for _, d in missing],
                    }
                ).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + key,
                },
            )
            with client.open(request, timeout=10) as r:
                raw = r.read(32769)
            if len(raw) > 32768:
                raise b.BenchmarkError("Oversized scoring response")
            graded = scores(json.loads(raw), len(missing))
            elapsed = (time.monotonic() - start) * 1000
            saved.update({s: v for (s, _), v in zip(missing, graded)})
            b.save(scorefile, saved)
        for id, signature, _ in inputs:
            values[id] = saved[signature]
        for alpha in (0, 0.5, 0.75, 1):
            ranking = blended(hits, values, alpha)
            out.append(
                {
                    **row,
                    "config_id": row["config_id"] + f"-r{alpha:g}",
                    "raw": ranking,
                    "grouped": ranking,
                    "rerank_alpha": alpha,
                    "rerank_ms_uncached_pairs": elapsed,
                    "uncached_pairs": len(missing),
                }
            )
        print(qid, row["surface"], row["config_id"], "scored", flush=True)
    (root / "ranked.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out)
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect")
    for name in (
        "out",
        "queries",
        "configs",
        "credentials",
        "es-url",
        "es-ca-file",
        "embedding-url",
        "archive-sha",
    ):
        c.add_argument("--" + name, required=True)
    c.add_argument("--node-image", default="eqarchives-benchmark-node")
    c.add_argument("--embedding-cache")
    c = sub.add_parser("rank")
    for name in ("run", "tokenizer", "key"):
        c.add_argument("--" + name, required=True)
    c.add_argument("--url", default="http://10.42.0.1:8083")
    args = p.parse_args()
    collect(args) if args.command == "collect" else rank(args)
