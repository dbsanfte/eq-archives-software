"""Paired HTTP measurements against llama.cpp using private archive passages.

Input is the source-linked evidence fixture from the existing search benchmark.
Results contain aggregate diagnostics, never source texts, vectors or credentials.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import statistics
import threading
import time

from backend import EMBED_MODEL, RERANK_MODEL, PINS, PREFIX, SUFFIX, verified


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1)]


def checked_values(kind, data, expected):
    """Malformed/nonfinite comparator output must never pass a quality gate."""
    model = EMBED_MODEL if kind == "embed" else RERANK_MODEL
    if not isinstance(data, dict) or data.get("model") != model:
        raise RuntimeError("Unexpected benchmark response model")
    rows = data.get("data" if kind == "embed" else "results")
    if (
        not isinstance(rows, list)
        or len(rows) != expected
        or any(not isinstance(row, dict) or type(row.get("index")) is not int for row in rows)
    ):
        raise RuntimeError("Unexpected benchmark response identity")
    ordered = sorted(rows, key=lambda row: row["index"])
    if [row["index"] for row in ordered] != list(range(expected)):
        raise RuntimeError("Unexpected benchmark response identity")
    values = []
    for row in ordered:
        value = row.get("embedding" if kind == "embed" else "relevance_score")
        if kind == "embed":
            valid = (
                isinstance(value, list)
                and len(value) == 768
                and all(type(v) in (float, int) and math.isfinite(v) and abs(v) <= 1 for v in value)
                and any(value)
            )
        else:
            valid = type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1
        if not valid:
            raise RuntimeError("Invalid benchmark vector or relevance score")
        values.append(value)
    return values


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixtures", type=Path, required=True)
    p.add_argument("--root", type=Path, default=Path("/models"))
    p.add_argument("--native", default="http://127.0.0.1:8084")
    p.add_argument("--llama-embed", default="http://192.168.68.108:8081")
    p.add_argument("--llama-rerank", default="http://127.0.0.1:8083")
    p.add_argument("--embed-key", type=Path, default=Path("/run/secrets/embed_key"))
    p.add_argument("--rerank-key", type=Path, default=Path("/run/secrets/rerank_key"))
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--kinds", nargs="+", choices=("embed", "rerank"), default=["embed", "rerank"])
    p.add_argument(
        "--diagnostic",
        action="store_true",
        help="Retain failed compatibility receipts and continue; never marks them passing",
    )
    args = p.parse_args()
    if not 2 <= args.repeats <= 20:
        p.error("Use 2–20 bounded repetitions")
    from tokenizers import Tokenizer
    import httpx

    tokenizers = {
        kind: Tokenizer.from_file(str(verified(args.root / pin[2], pin[3]))) for kind, pin in PINS.items()
    }
    for tokenizer in tokenizers.values():
        tokenizer.no_padding()
        tokenizer.no_truncation()
    fixture = json.loads(args.fixtures.read_text())
    cases = []
    for item in fixture[:6]:
        documents, embeddings = [], []
        for identity in item["ids"][:60]:
            evidence = item["evidence"].get(identity, {})
            passage = evidence.get("passages", {}).get("semantic") or evidence.get("passages", {}).get(
                "lexical"
            )
            if not passage or not isinstance(passage.get("text_chunk"), str):
                continue
            text = "\n".join(str(passage.get(k) or "") for k in ("title", "context", "text_chunk"))
            tokenizer = tokenizers["rerank"]
            allowance = (
                768
                - len(
                    tokenizer.encode(
                        PREFIX + item["query"] + "\n<Document>: " + SUFFIX, add_special_tokens=False
                    ).ids
                )
                - 8
            )
            doc = tokenizer.decode(tokenizer.encode(text, add_special_tokens=False).ids[:allowance])
            if (
                len(
                    tokenizer.encode(
                        PREFIX + item["query"] + "\n<Document>: " + doc + SUFFIX, add_special_tokens=False
                    ).ids
                )
                > 768
            ):
                raise RuntimeError("Benchmark prompt exceeds the live contract")
            documents.append(doc)
            tokenizer = tokenizers["embed"]
            inp = "title: none | text: " + text
            inp = tokenizer.decode(tokenizer.encode(inp, add_special_tokens=False).ids[:479])
            if len(tokenizer.encode(inp).ids) > 512:
                raise RuntimeError("Benchmark embedding exceeds the live contract")
            embeddings.append(inp)
        if documents:
            cases.append({"query": item["query"], "documents": documents, "embeddings": embeddings})
    keys = {"embed": args.embed_key.read_text().strip(), "rerank": args.rerank_key.read_text().strip()}
    clients = {
        kind: httpx.Client(timeout=30, trust_env=False, headers={"Authorization": "Bearer " + key})
        for kind, key in keys.items()
    }
    rerank_slots = threading.BoundedSemaphore(4)

    def request(kind, url, body):
        reply = clients[kind].post(url + ("/v1/embeddings" if kind == "embed" else "/v1/rerank"), json=body)
        reply.raise_for_status()
        data = reply.json()
        expected = len(body["input"] if kind == "embed" else body["documents"])
        return checked_values(kind, data, expected)

    def call(kind, url, body):
        start = time.perf_counter()
        if kind == "embed":
            values = request(kind, url, body)
        else:
            # Match the live adapter: complete-prompt deduplication, <=20
            # documents per request and four shared in-flight HTTP batches.
            unique = list(dict.fromkeys(body["documents"]))
            batches = [unique[i : i + 20] for i in range(0, len(unique), 20)]

            def submit(documents):
                with rerank_slots:
                    return request(kind, url, dict(body, documents=documents))

            with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
                scores = [v for result in pool.map(submit, batches) for v in result]
            mapping = dict(zip(unique, scores))
            values = [mapping[document] for document in body["documents"]]
        return (time.perf_counter() - start) * 1000, values

    reports = []
    try:
        for kind, counts in (("embed", (1, 8, 32)), ("rerank", (20, 60))):
            if kind not in args.kinds:
                continue
            for count in counts:
                for concurrency in (1, 2):
                    times = {"native": [], "llama": []}
                    errors, cosines, overlaps, worst, unique_counts = [], [], [], [], []
                    for repeat in range(args.repeats + 1):
                        bodies = []
                        for client_index in range(concurrency):
                            item = cases[(repeat + client_index) % len(cases)]
                            bodies.append(
                                (
                                    {
                                        "model": EMBED_MODEL,
                                        "input": (
                                            ["task: search result | query: " + item["query"]]
                                            if count == 1
                                            else item["embeddings"][:count]
                                        ),
                                    }
                                    if kind == "embed"
                                    else {
                                        "model": RERANK_MODEL,
                                        "query": item["query"],
                                        "documents": item["documents"][:count],
                                    }
                                )
                            )
                            if kind == "rerank":
                                unique_counts.append(len(set(bodies[-1]["documents"])))
                        replies = {}
                        # Alternate order to reduce clock/thermal/order bias. Neither
                        # backend runs simultaneously with its comparator.
                        for backend in (("native", "llama") if repeat % 2 == 0 else ("llama", "native")):
                            url = (
                                args.native
                                if backend == "native"
                                else (args.llama_embed if kind == "embed" else args.llama_rerank)
                            )
                            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                                results = list(pool.map(lambda body: call(kind, url, body), bodies))
                            replies[backend] = [result[1] for result in results]
                            if repeat:
                                times[backend].extend(latency for latency, _ in results)
                        for client_index, (native, llama) in enumerate(
                            zip(replies["native"], replies["llama"])
                        ):
                            for a, b in zip(native, llama):
                                if kind == "embed":
                                    cosine = sum(x * y for x, y in zip(a, b)) / math.sqrt(
                                        sum(x * x for x in a) * sum(y * y for y in b)
                                    )
                                    cosines.append(cosine)
                                else:
                                    errors.append(abs(a - b))
                            if kind == "rerank":
                                k = min(10, len(native))
                                top = lambda values: set(
                                    sorted(range(len(values)), key=lambda i: (-values[i], i))[:k]
                                )
                                overlaps.append(len(top(native) & top(llama)) / k)
                                worst.extend(
                                    {
                                        "case": (repeat + client_index) % len(cases),
                                        "index": i,
                                        "native": x,
                                        "llama": y,
                                        "absolute_error": abs(x - y),
                                    }
                                    for i, (x, y) in enumerate(zip(native, llama))
                                )
                    row = {
                        "kind": kind,
                        "count": count,
                        "concurrency": concurrency,
                        "latency_ms": {
                            k: {"p50": statistics.median(v), "p95": percentile(v, 0.95), "samples": len(v)}
                            for k, v in times.items()
                        },
                        "speedup_p50": statistics.median(times["llama"]) / statistics.median(times["native"]),
                    }
                    if cosines:
                        row["min_cosine"] = min(cosines)
                    if errors:
                        row["unique_prompts"] = {"min": min(unique_counts), "max": max(unique_counts)}
                        row["http_batch_size"] = 20
                        row["shared_http_slots"] = 4
                        row["max_abs_score_error"] = max(errors)
                        row["mean_top10_overlap"] = statistics.mean(overlaps)
                        row["worst"] = sorted(worst, key=lambda r: -r["absolute_error"])[:3]
                    row["compatible"] = not (
                        (cosines and min(cosines) < 0.999) or (errors and max(errors) > 0.03)
                    )
                    reports.append(row)
                    args.output.write_text(json.dumps(reports, indent=2) + "\n")
                    print(json.dumps(row), flush=True)
                    if not row["compatible"] and not args.diagnostic:
                        raise RuntimeError("Compatibility gate failed; retained partial benchmark receipt")
    finally:
        for client in clients.values():
            client.close()


if __name__ == "__main__":
    main()
