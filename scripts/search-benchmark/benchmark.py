#!/usr/bin/env python3
"""Read-only, reproducible relevance experiments against the archive index."""

import argparse
import base64
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
VERSION = "search-benchmark-v1"
METADATA = ["id", "title", "url", "parent_id", "domain_name", "mailing_list_name", "capture_date", "llm_guessed_date"]
FILTER_FIELDS = {"domain_name", "mailing_list_name", "file_type", "mime_type", "llm_tags", "llm_content_flavour", "capture_date", "llm_guessed_date"}


class BenchmarkError(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def jsonlines(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def validate_suite(queries, configs):
    for rows in (queries, configs):
        ids = [row["id"] for row in rows]
        if not ids or len(ids) != len(set(ids)):
            raise BenchmarkError("Suite IDs must be nonempty and unique")
    for query in queries:
        if not query.get("text", "").strip() or query.get("split") not in ("tune", "validation"):
            raise BenchmarkError("Queries need text and a tune/validation split")
        for f in query.get("filters", []):
            if f["field"] not in FILTER_FIELDS or f["type"] not in ("all", "any", "none", "range") or not f["values"]:
                raise BenchmarkError("Unsupported query filter")
            if f["field"] in ("capture_date", "llm_guessed_date"):
                for value in f["values"]:
                    if not isinstance(value, dict) or not value or set(value) - {"from", "to", "name", "isDateField"} or value.get("isDateField") is not True or not isinstance(value.get("name"), str) or f["type"] != "range":
                        raise BenchmarkError("Date filters require explicit UTC ranges")
                    for key in ("from", "to"):
                        if key in value:
                            date = datetime.fromisoformat(value[key].replace("Z", "+00:00"))
                            if date.utcoffset() is None or date.utcoffset().total_seconds() != 0:
                                raise BenchmarkError("Date filters must use UTC")
    for config in configs:
        if config["mode"] not in ("lexical", "semantic", "hybrid", "rrf"):
            raise BenchmarkError("Unsupported retrieval mode")
        k, candidates = config.get("k", 10), config.get("num_candidates", 100)
        if not isinstance(k, int) or not isinstance(candidates, int) or not 1 <= k <= candidates <= 10000:
            raise BenchmarkError("Require 1 <= k <= num_candidates <= 10000")
        if config.get("boost", 5) <= 0 or config.get("rrf_constant", 60) <= 0:
            raise BenchmarkError("Boost and RRF constant must be positive")
        if "similarity" in config and not -1 <= config["similarity"] <= 1:
            raise BenchmarkError("Cosine threshold must be between -1 and 1")


class HTTP:
    """Credentials stay in memory; errors never include response bodies or headers."""

    def __init__(self, base, headers=None, pause=0):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BenchmarkError("Use an HTTP endpoint without embedded credentials or a query")
        self.base = base.rstrip("/")
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.pause = pause

    def request(self, method, path, body=None):
        # Disable redirects so a misconfigured upstream cannot forward credentials.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        for attempt in range(3):
            try:
                request = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=self.headers, method=method)
                with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
                    result = json.load(response)
                if self.pause:
                    time.sleep(self.pause)
                return result
            except urllib.error.HTTPError as error:
                if error.code in (429, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise BenchmarkError(f"HTTP operation failed (status {error.code}); upstream body omitted") from None
            except (urllib.error.URLError, TimeoutError, ValueError):
                raise BenchmarkError("HTTP operation failed; upstream details omitted") from None


class Elasticsearch(HTTP):
    def request(self, method, path, body=None):
        endpoint = path.split("?")[0]
        read = method == "GET" and (endpoint in ("/", "/_license") or endpoint.endswith(("/_mapping", "/_settings")))
        read |= method == "POST" and (endpoint == "/_search" or endpoint.endswith("/_pit"))
        read |= method == "DELETE" and endpoint == "/_pit"
        if not read:
            raise BenchmarkError("Benchmark transport refuses this Elasticsearch operation")
        return super().request(method, path, body)


class Frontend:
    """One network-disabled Node process loads the actual frontend and lockfile."""

    def __init__(self, image):
        self.process = subprocess.Popen([
            "docker", "run", "--rm", "-i", "--network=none",
            "-e", "NODE_ENV=production", "-e", "NODE_PATH=/app/node_modules",
            "-e", "BROWSERSLIST_IGNORE_OLD_DATA=true", "-v", f"{ROOT}:/repo:ro",
            image, "node", "/repo/elastic-indexer-stack/frontend/scripts/search-benchmark.cjs",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def call(self, value):
        try:
            self.process.stdin.write(json.dumps(value) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
            result = json.loads(line)
        except (BrokenPipeError, ValueError):
            raise BenchmarkError("Frontend bridge stopped") from None
        if "error" in result:
            raise BenchmarkError(result["error"])
        return result["result"]

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=15)


class Snapshot:
    def __init__(self, es, index, max_seconds=900):
        self.es, self.index, self.max_seconds = es, index, max_seconds
        self.pit = None

    def __enter__(self):
        self.started = time.monotonic()
        self.pit = self.es.request("POST", f"/{urllib.parse.quote(self.index, safe='')}/_pit?keep_alive=2m")["id"]
        return self

    def search(self, body):
        if time.monotonic() - self.started > self.max_seconds:
            raise BenchmarkError("Snapshot time budget exhausted; start a fresh run")
        request = deepcopy(body)
        request["pit"] = {"id": self.pit, "keep_alive": "2m"}
        request["timeout"] = "30s"
        response = self.es.request("POST", "/_search", request)
        self.pit = response.get("pit_id", self.pit)
        if response.get("timed_out") or response.get("_shards", {}).get("failed"):
            raise BenchmarkError("Rejecting incomplete Elasticsearch results")
        return response

    def __exit__(self, *args):
        if self.pit:
            self.es.request("DELETE", "/_pit", {"id": self.pit})


def validate_vector(vector):
    if len(vector) != 768 or any(type(n) not in (int, float) or not math.isfinite(n) for n in vector):
        raise BenchmarkError("Expected a finite 768-dimensional vector")
    norm = math.sqrt(sum(n * n for n in vector))
    if abs(norm - 1) > 0.002:
        raise BenchmarkError("Expected a normalized Nomic vector")
    return vector


def embedding(client, model, text, cache):
    key = digest({"model": model, "input": "search_query: " + text})
    path = cache / (key + ".json")
    if path.exists():
        return validate_vector(load(path)["vector"]), 0, True
    started = time.perf_counter()
    response = client.request("POST", "/embeddings", {"model": model, "input": ["search_query: " + text], "encoding_format": "float"})
    if response.get("model") != model or len(response.get("data", [])) != 1:
        raise BenchmarkError("Embedding service contract changed")
    vector = validate_vector(response["data"][0]["embedding"])
    elapsed = (time.perf_counter() - started) * 1000
    save(path, {"vector": vector, "model": model, "input_sha256": key})
    return vector, elapsed, False


def build_body(exported, config, cohort, defaults):
    body = deepcopy(exported["body"])
    restrictions = [cohort] if cohort else []
    if body.get("post_filter"):
        restrictions.append(body["post_filter"])
    body["query"] = {"bool": {"must": [body["query"]], "filter": restrictions}}
    if exported["semantic_allowed"] and config["mode"] != "lexical":
        branches = body.get("knn", [])
        if not branches:
            raise BenchmarkError("Semantic query lacks the frontend embedding branch")
        selected = config.get("vector_fields")
        if selected is not None:
            branches = [branch for branch in branches if branch["field"] in selected]
        if not branches:
            raise BenchmarkError("No selected vector field exists in the frontend contract")
        for branch in branches:
            branch["boost"] = config.get("boost", defaults["boost"]) * (selected[branch["field"]] if selected else 1)
            if "similarity" in config:
                branch["similarity"] = config["similarity"]
            # Production already prefilters facets. Add the fixed cohort too.
            existing = branch.get("filter", [])
            if not isinstance(existing, list):
                existing = [existing]
            branch["filter"] = existing + ([cohort] if cohort else [])
        body["knn"] = branches
        if config["mode"] == "semantic":
            del body["query"]
    else:
        body.pop("knn", None)
    body["track_total_hits"] = False
    return body


def filter_matches(source, filters):
    for f in filters:
        actual = source.get(f["field"])
        values = actual if isinstance(actual, list) else [actual]
        checks = []
        for expected in f["values"]:
            if isinstance(expected, dict):
                def within(value):
                    if value is None:
                        return False
                    date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    return all((date >= datetime.fromisoformat(expected["from"].replace("Z", "+00:00")) if key == "from" else date <= datetime.fromisoformat(expected["to"].replace("Z", "+00:00"))) for key in ("from", "to") if key in expected)
                checks.append(any(within(value) for value in values))
            else:
                checks.append(expected in values)
        matched = {"all": all(checks), "any": any(checks), "none": not any(checks), "range": all(checks)}[f["type"]]
        if not matched:
            return False
    return True


def grouped(hits):
    seen = set()
    result = []
    for hit in hits:
        key = hit["capture_key"]
        if key not in seen:
            seen.add(key)
            result.append(hit)
    return result


def retrieve(snapshot, frontend, body, query, depth, pause):
    hits, took, elapsed = [], 0, 0
    exhausted = False
    # Match the frontend's metadata batches and 1,000-capture browsing bound.
    while len(hits) < 1000 and len(grouped(hits)) <= depth:
        request = deepcopy(body)
        request.update(size=50, **{"from": len(hits)})
        started = time.perf_counter()
        response = snapshot.search(request)
        elapsed += (time.perf_counter() - started) * 1000
        took += response.get("took", 0)
        batch = response["hits"]["hits"]
        keys = frontend.call({"op": "keys", "hits": batch})
        for hit, key in zip(batch, keys):
            if not filter_matches(hit.get("_source", {}), query.get("filters", [])):
                raise BenchmarkError("Result escaped requested source/date filters")
            hits.append({"_id": hit["_id"], "_index": hit["_index"], "_score": hit.get("_score"), "_source": hit.get("_source", {}), "capture_key": key})
        if len(batch) < 50:
            exhausted = True
            break
        time.sleep(pause)
    return {"raw": hits, "grouped": grouped(hits)[:depth], "es_ms": took, "request_ms": elapsed,
            "scanned": len(hits), "window_reached": len(hits) >= 1000 and not exhausted}


def rrf(first, second, constant):
    scores, documents = defaultdict(float), {}
    for ranking in (first, second):
        seen = set()
        for rank, hit in enumerate(ranking, 1):
            if hit["_id"] in seen:
                continue
            seen.add(hit["_id"])
            scores[hit["_id"]] += 1 / (constant + rank)
            documents.setdefault(hit["_id"], hit)
    ordered = sorted(scores, key=lambda key: (-scores[key], key))
    return [{**documents[key], "_score": scores[key]} for key in ordered]


def provenance(contract, es, index):
    files = sorted(HERE.glob("*.py")) + [ROOT / "elastic-indexer-stack/frontend/scripts/search-benchmark.cjs", ROOT / "elastic-indexer-stack/frontend/yarn.lock",
                                      ROOT / "elastic-indexer-stack/k8s-manifests/embeddings/runtime.env"]
    files += sorted((ROOT / "elastic-indexer-stack/frontend/src/search").glob("*.js"))
    files += [ROOT / "elastic-indexer-stack/frontend/src/config/engine.json", ROOT / "elastic-indexer-stack/frontend/src/views/search/AdvancedSettings.js"]
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    return {"benchmark_version": VERSION, "software_git_sha": sha, "source_hashes": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
            "frontend_contract": contract, "elasticsearch_version": es.request("GET", "/")["version"]["number"],
            "mappings": es.request("GET", f"/{index}/_mapping"), "settings": es.request("GET", f"/{index}/_settings")}


def run(args):
    queries, configs = load(args.queries), load(args.configs)
    validate_suite(queries, configs)
    if args.query_ids:
        queries = [q for q in queries if q["id"] in args.query_ids.split(",")]
    if args.config_ids:
        configs = [c for c in configs if c["id"] in args.config_ids.split(",")]
    if not queries or not configs or len(queries) * len(configs) > args.max_cases:
        raise BenchmarkError("Selected suite is empty or exceeds --max-cases")
    output = Path(args.out)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise BenchmarkError("Use a fresh run directory; each retrieval run needs a new snapshot")
    auth = load(args.credentials) if args.credentials else {}
    es_headers = {}
    if auth.get("es_username"):
        es_headers["Authorization"] = "Basic " + base64.b64encode((auth["es_username"] + ":" + auth["es_password"]).encode()).decode()
    model_headers = {"Authorization": "Bearer " + auth["embedding_api_key"]} if auth.get("embedding_api_key") else {}
    es, embeddings = Elasticsearch(args.es_url, es_headers), HTTP(args.embedding_url, model_headers, pause=args.pause)
    frontend = Frontend(args.node_image)
    try:
        contract = frontend.call({"op": "contract"})
        model = contract["engine"]["embeddingModel"]
        metadata = provenance(contract, es, args.index)
        metadata.update(started_at=datetime.now(timezone.utc).isoformat(), status="running", queries=queries, configs=configs, depth=args.depth,
                        archive_git_sha=args.archive_sha, chunking_version=args.chunking_version,
                        limitation="Pilot cohort; pooled relevance, not exhaustive corpus recall. Timing includes concurrent indexing load.")
        save(output / "manifest.json", metadata)
        cache = Path(args.embedding_cache) if args.embedding_cache else output / "embedding-cache"
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        vectors = {}
        # Embed once per query before opening the short-lived ES snapshot.
        for query in queries:
            base = frontend.call({"op": "build", "query": query})
            if base["semantic_allowed"] and any(c["mode"] != "lexical" for c in configs):
                vector, elapsed, reused = embedding(embeddings, model, query["text"], cache)
                vectors[query["id"]] = vector
                print(f"Embedded {query['id']} ({'cached' if reused else str(round(elapsed)) + ' ms'})", flush=True)
        cohort = {"term": {"text_chunking_version": args.chunking_version}} if args.chunking_version else None
        pool = defaultdict(dict)
        generator = random.Random(args.seed)
        with Snapshot(es, args.index, args.max_seconds) as snapshot, (output / "runs.jsonl").open("w") as stream:
            stats = snapshot.search({"size": 0, "track_total_hits": True, "query": cohort or {"match_all": {}}, "aggs": {
                "lists": {"terms": {"field": "mailing_list_name", "size": 20}},
                "domains": {"terms": {"field": "domain_name", "size": 20}},
                "chunking_versions": {"terms": {"field": "text_chunking_version", "size": 10, "missing": "unversioned"}},
                "extraction_versions": {"terms": {"field": "text_extraction_version", "size": 10, "missing": "unversioned"}}
            }})
            metadata["cohort_count"] = stats["hits"]["total"]
            metadata["cohort_aggregations"] = stats["aggregations"]
            save(output / "manifest.json", metadata)
            requests = []
            for query in queries:
                order = list(configs)
                generator.shuffle(order)
                for config in order:
                    params = {key: config[key] for key in ("k", "num_candidates", "boost") if key in config}
                    exported = frontend.call({"op": "build", "query": query, "fields": config.get("lexical_fields"), "params": params,
                                              "vector": vectors.get(query["id"]) if config["mode"] != "lexical" else None})
                    body = build_body(exported, config, cohort, contract["defaults"])
                    if config["mode"] == "rrf" and exported["semantic_allowed"]:
                        lexical = deepcopy(body)
                        lexical.pop("knn", None)
                        semantic = deepcopy(body)
                        semantic.pop("query", None)
                        a = retrieve(snapshot, frontend, lexical, query, args.depth, args.pause)
                        b = retrieve(snapshot, frontend, semantic, query, args.depth, args.pause)
                        raw = rrf(a["raw"], b["raw"], config.get("rrf_constant", 60))
                        result = {"raw": raw, "grouped": grouped(raw)[:args.depth], "es_ms": a["es_ms"] + b["es_ms"],
                                  "request_ms": a["request_ms"] + b["request_ms"], "scanned": a["scanned"] + b["scanned"], "window_reached": a["window_reached"] or b["window_reached"]}
                    else:
                        result = retrieve(snapshot, frontend, body, query, args.depth, args.pause)
                    result.update(query_id=query["id"], config_id=config["id"], semantic_used=bool(body.get("knn")))
                    stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                    stream.flush()
                    requests.append({"query_id": query["id"], "config_id": config["id"], "body": body})
                    for hit in result["raw"][:args.depth] + result["grouped"]:
                        pool[query["id"]][hit["_id"]] = hit
                    print(f"{query['id']} {config['id']}: {len(result['grouped'])} unique results, {round(result['request_ms'])} ms", flush=True)
            # Include explicitly known targets even if every configuration missed them.
            for query in queries:
                targets = query.get("known_relevant_ids", [])
                if targets:
                    missing = [key for key in targets if key not in pool[query["id"]]]
                    if missing:
                        fetched = snapshot.search({"size": len(missing), "_source": METADATA, "query": {"bool": {"filter": [cohort or {"match_all": {}}, {"ids": {"values": missing}}]}}})["hits"]["hits"]
                        keys = frontend.call({"op": "keys", "hits": fetched})
                        for hit, key in zip(fetched, keys):
                            if filter_matches(hit["_source"], query.get("filters", [])):
                                pool[query["id"]][hit["_id"]] = {**hit, "capture_key": key}
                        if any(key not in pool[query["id"]] for key in targets):
                            raise BenchmarkError("Known relevant ID is absent from the snapshot cohort or filters")
            all_ids = sorted({key for documents in pool.values() for key in documents})
            sources = {}
            for start in range(0, len(all_ids), 30):
                batch = all_ids[start:start + 30]
                fetched = snapshot.search({"size": len(batch), "_source": METADATA + ["text_full", "llm_image_text_full"], "query": {"ids": {"values": batch}}})["hits"]["hits"]
                for hit in fetched:
                    source = hit["_source"]
                    sources[hit["_id"]] = {"source": source, "source_sha256": digest(source)}
                time.sleep(args.pause)
            if len(sources) != len(all_ids):
                raise BenchmarkError("A pooled document is missing from the snapshot")
            save(output / "sources.json", sources)
            save(output / "pool.json", {qid: list(documents.values()) for qid, documents in pool.items()})
            save(output / "requests.json", requests)
        metadata.update(status="complete", completed_at=datetime.now(timezone.utc).isoformat(), pooled_documents=len(sources))
        save(output / "manifest.json", metadata)
        print(f"Retrieval complete: {len(queries)} queries, {len(configs)} configurations, {len(sources)} pooled sources", flush=True)
    except Exception:
        if (output / "manifest.json").exists():
            metadata = load(output / "manifest.json")
            metadata["status"] = "failed"
            save(output / "manifest.json", metadata)
        raise
    finally:
        frontend.close()


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("run", help="Collect results and complete source text from one ES snapshot")
    collect.add_argument("--es-url", required=True)
    collect.add_argument("--embedding-url", required=True, help="OpenAI-compatible Nomic endpoint, ending in /v1")
    collect.add_argument("--credentials", help="Private JSON file: es_username, es_password, embedding_api_key")
    collect.add_argument("--index", default="eq-archive")
    collect.add_argument("--out", required=True)
    collect.add_argument("--queries", default=str(HERE / "queries.json"))
    collect.add_argument("--configs", default=str(HERE / "configs.json"))
    collect.add_argument("--query-ids")
    collect.add_argument("--config-ids")
    collect.add_argument("--node-image", default="eqarchives-benchmark-node")
    collect.add_argument("--chunking-version", default="nomic-paragraphs-480-overlap48-v1", help="Empty string selects the entire index")
    collect.add_argument("--archive-sha", required=True)
    collect.add_argument("--embedding-cache")
    collect.add_argument("--depth", type=int, default=50, choices=range(10, 101))
    collect.add_argument("--max-cases", type=int, default=800)
    collect.add_argument("--max-seconds", type=int, default=900)
    collect.add_argument("--pause", type=float, default=0.2)
    collect.add_argument("--seed", type=int, default=20261005)
    judge = sub.add_parser("grade", help="Explicitly run paid Luna judgments on complete, pooled sources")
    judge.add_argument("--run", required=True)
    judge.add_argument("--api-key-file", required=True)
    judge.add_argument("--batch-size", type=int, default=8, choices=range(1, 17))
    judge.add_argument("--max-input-characters", type=int, default=20_000_000)
    judge.add_argument("--max-source-characters", type=int, default=200_000)
    judge.add_argument("--pause", type=float, default=0.3)
    evaluate = sub.add_parser("report", help="Score saved rankings against explicit judgments")
    evaluate.add_argument("--run", required=True)
    evaluate.add_argument("--ratings", nargs="*")
    evaluate.add_argument("--allow-model-ratings", action="store_true")
    evaluate.add_argument("--baseline", default="current-hybrid")
    human = sub.add_parser("review", help="Export a blind, offline human review page")
    human.add_argument("--run", required=True)
    nearest = sub.add_parser("ann", help="Compare kNN with exact, max-chunk cosine on a bounded sample")
    nearest.add_argument("--run", required=True)
    nearest.add_argument("--es-url", required=True)
    nearest.add_argument("--embedding-url", required=True)
    nearest.add_argument("--credentials", required=True)
    nearest.add_argument("--index", default="eq-archive")
    nearest.add_argument("--embedding-cache")
    nearest.add_argument("--query-ids", default="q007,q009,q013,q021")
    nearest.add_argument("--sample-size", type=int, default=512, choices=range(20, 2001))
    nearest.add_argument("--k", type=int, default=10)
    nearest.add_argument("--candidates", nargs="+", type=int, default=[50, 100, 250, 500])
    nearest.add_argument("--pause", type=float, default=0.2)
    nearest.add_argument("--seed", type=int, default=20261005)
    return root


def main():
    args = parser().parse_args()
    try:
        if args.command == "run":
            run(args)
        elif args.command == "grade":
            from grade import grade
            grade(args)
        elif args.command == "ann":
            from ann import ann
            if not 1 <= args.k <= args.sample_size or any(not args.k <= n <= 10000 for n in args.candidates):
                raise BenchmarkError("ANN requires k <= sample size and k <= num_candidates <= 10000")
            ann(args)
        else:
            from evaluate import report, review
            {"report": report, "review": review}[args.command](args)
    except BenchmarkError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
