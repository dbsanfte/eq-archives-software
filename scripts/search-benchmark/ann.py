"""Bounded nearest-neighbour diagnostic; does not establish human relevance."""

import base64
import math
from pathlib import Path
import time

from benchmark import BenchmarkError, Elasticsearch, HTTP, Snapshot, digest, embedding, load, save


def cosine(left, right):
    norm = math.sqrt(sum(v * v for v in left) * sum(v * v for v in right))
    if norm == 0:
        raise BenchmarkError("Zero vector in ANN sample")
    return sum(a * b for a, b in zip(left, right)) / norm


def exact_documents(hits, vector):
    ranking = []
    for hit in hits:
        chunks = [chunk["vector"] for chunk in hit["_source"].get("text", []) if chunk.get("vector")]
        if any(len(chunk) != len(vector) for chunk in chunks):
            raise BenchmarkError("ANN sample vector dimensions differ")
        if chunks:
            ranking.append((hit["_id"], max(cosine(vector, chunk) for chunk in chunks)))
    return sorted(ranking, key=lambda item: (-item[1], item[0]))


def ann(args):
    directory = Path(args.run)
    manifest = load(directory / "manifest.json")
    auth = load(args.credentials)
    es = Elasticsearch(args.es_url, {"Authorization": "Basic " + base64.b64encode((auth["es_username"] + ":" + auth["es_password"]).encode()).decode()})
    cache = Path(args.embedding_cache) if args.embedding_cache else directory / "embedding-cache"
    client = HTTP(args.embedding_url, {"Authorization": "Bearer " + auth["embedding_api_key"]})
    model = manifest["frontend_contract"]["engine"]["embeddingModel"]
    queries = [q for q in manifest["queries"] if q["id"] in args.query_ids.split(",")]
    vectors = {q["id"]: embedding(client, model, q["text"], cache)[0] for q in queries}
    cohort = {"term": {"text_chunking_version": manifest["chunking_version"]}} if manifest["chunking_version"] else {"match_all": {}}
    output = {"sample_size": args.sample_size, "k": args.k, "results": [], "limitation":
              "Fixed filtered sample. Elasticsearch may switch filtered HNSW to brute force. Results measure this bounded diagnostic, not full-index ANN recall or relevance."}
    with Snapshot(es, args.index, max_seconds=300) as snapshot:
        sample = snapshot.search({"size": args.sample_size, "_source": ["text"], "query": {"function_score": {
            "query": {"bool": {"filter": [cohort, {"nested": {"path": "text", "query": {"exists": {"field": "text.vector"}}}}]}},
            "random_score": {"seed": args.seed, "field": "_seq_no"}, "boost_mode": "replace"}}})["hits"]["hits"]
        if len(sample) < args.k:
            raise BenchmarkError("ANN sample has fewer documents than k")
        output["actual_sample_size"] = len(sample)
        output["sample_sha256"] = digest(sample)
        save(directory / "ann-sample.json", sample)
        for query in queries:
            exact = exact_documents(sample, vectors[query["id"]])
            gold = {doc for doc, score in exact[:args.k]}
            cutoff = exact[args.k - 1][1]
            tied = {doc for doc, score in exact if score >= cutoff - 1e-6}
            for candidates in args.candidates:
                started = time.perf_counter()
                result = snapshot.search({"size": args.k, "_source": False,
                    "knn": {"field": "text.vector", "query_vector": vectors[query["id"]], "k": args.k, "num_candidates": candidates,
                            "filter": {"ids": {"values": [hit["_id"] for hit in sample]}}},
                    "sort": [{"_score": "desc"}, {"id": "asc"}]})
                actual = {hit["_id"] for hit in result["hits"]["hits"]}
                output["results"].append({"query_id": query["id"], "num_candidates": candidates,
                    "strict_id_recall_at_k": len(gold & actual) / args.k, "tie_aware_recall_at_k": len(tied & actual) / args.k,
                    "es_ms": result.get("took"), "request_ms": (time.perf_counter() - started) * 1000})
                time.sleep(args.pause)
    save(directory / "ann-report.json", output)
    print(f"ANN diagnostic saved: {len(sample)} documents, {len(queries)} queries, {len(args.candidates)} candidate counts; filtered-sample limitation recorded.")
