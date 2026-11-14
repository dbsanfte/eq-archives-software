"""Measure a short search arriving while a top-60 search is already in flight.

Uses the live client's four shared HTTP slots and 20-document payloads. Keep
private fixtures, credentials and receipts outside Git. No paid calls or writes.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import threading
import time

from backend import RERANK_MODEL
from benchmark_http import checked_values, load_cases, percentile, rerank_batches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("/models"))
    parser.add_argument("--current", default="http://127.0.0.1:8084")
    parser.add_argument("--candidate", default="http://127.0.0.1:8085")
    parser.add_argument("--llama", default="http://127.0.0.1:8083")
    parser.add_argument("--key", type=Path, default=Path("/run/secrets/rerank_key"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--short-documents", nargs="+", type=int, choices=(1, 8, 20), default=[1, 8, 20])
    args = parser.parse_args()
    if not 2 <= args.repeats <= 12:
        parser.error("Use 2–12 bounded repetitions")
    import httpx

    cases = load_cases(args.fixtures, args.root)
    if len(cases) < 2:
        parser.error("Require two distinct query cases")
    slots = threading.BoundedSemaphore(4)
    urls = {key: getattr(args, key) for key in ("current", "candidate", "llama")}
    reports = []
    with httpx.Client(timeout=15, trust_env=False,
                      headers={"Authorization": "Bearer " + args.key.read_text().strip()}) as client:
        def search(url, case, count, started=None):
            start = time.perf_counter()
            docs = case["documents"][:count]
            unique = list(dict.fromkeys(docs))
            batches = rerank_batches(case["query"], unique)

            def submit(batch):
                with slots:
                    if started is not None:
                        started.set()
                    result = client.post(url + "/v1/rerank", json={
                        "model": RERANK_MODEL, "query": case["query"], "documents": batch})
                    result.raise_for_status()
                    return checked_values("rerank", result.json(), len(batch))

            with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
                values = [value for batch in pool.map(submit, batches) for value in batch]
            scores = dict(zip(unique, values))
            return (time.perf_counter() - start) * 1000, [scores[doc] for doc in docs]

        for count in args.short_documents:
            observations, difference = [], 0.0
            for repeat in range(args.repeats + 1):
                large, short = cases[repeat % len(cases)], cases[(repeat + 1) % len(cases)]
                order = list(urls)
                order = order[repeat % 3:] + order[:repeat % 3]
                measured, outputs = {}, {}
                for backend in order:
                    started = threading.Event()
                    start = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        large_future = pool.submit(search, urls[backend], large, 60, started)
                        if not started.wait(5):
                            raise RuntimeError("Large search failed to start")
                        time.sleep(0.02)
                        arrival = (time.perf_counter() - start) * 1000
                        short_future = pool.submit(search, urls[backend], short, count)
                        short_ms, short_values = short_future.result()
                        large_ms, large_values = large_future.result()
                    measured[backend] = {"short_ms": short_ms, "large_ms": large_ms,
                                         "arrival_ms": arrival,
                                         "completion_ms": (time.perf_counter() - start) * 1000}
                    outputs[backend] = short_values + large_values
                difference = max(difference, max(abs(a - b) for a, b in
                                 zip(outputs["current"], outputs["candidate"])))
                if repeat:
                    observations.append(measured)
            report = {
                "short_documents": count, "large_documents": 60, "pairs": args.repeats,
                "latency_ms": {backend: {metric: {
                    "p50": statistics.median(row[backend][metric] for row in observations),
                    "p95": percentile([row[backend][metric] for row in observations], 0.95)}
                    for metric in ("short_ms", "large_ms", "completion_ms")}
                    for backend in urls},
                "max_current_candidate_score_difference": difference,
                "observations": observations,
            }
            reports.append(report)
            args.output.write_text(json.dumps(reports, indent=2) + "\n")
            print(json.dumps({k: v for k, v in report.items() if k != "observations"}), flush=True)


if __name__ == "__main__":
    main()
