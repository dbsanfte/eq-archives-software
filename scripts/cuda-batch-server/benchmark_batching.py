"""Compare the same two complete HTTP requests concurrently and serially.

No paid judging, indexing writes or changes to model services. Fixtures and
receipts stay private. Each request uses the live client's bounded HTTP batches.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import random
import statistics
import threading
import time

from backend import RERANK_MODEL
from benchmark_http import checked_values, load_cases, rerank_batches


def paired_summary(serial, concurrent):
    if len(serial) != len(concurrent) or len(serial) < 2:
        raise ValueError("Require matched timing pairs")
    if any(not math.isfinite(t) or t <= 0 for t in serial + concurrent):
        raise ValueError("Invalid wall time")
    ratios = [a / b for a, b in zip(serial, concurrent)]
    logs = list(map(math.log, ratios))
    generator = random.Random(20261114)
    means = sorted(math.exp(statistics.mean(generator.choices(logs, k=len(logs)))) for _ in range(2000))
    return {
        "pairs": len(ratios),
        "serial_pair_median_ms": statistics.median(serial),
        "concurrent_pair_median_ms": statistics.median(concurrent),
        "paired_speedup_median": statistics.median(ratios),
        "paired_speedup_geometric_mean": math.exp(statistics.mean(logs)),
        "paired_speedup_ci95": [means[49], means[1949]],
        "concurrent_faster": means[49] > 1,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("/models"))
    parser.add_argument("--url", default="http://127.0.0.1:8084")
    parser.add_argument("--key", type=Path, default=Path("/run/secrets/rerank_key"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=8)
    args = parser.parse_args()
    if not 2 <= args.repeats <= 20:
        parser.error("Use 2–20 bounded repetitions")
    cases = load_cases(args.fixtures, args.root)
    if len(cases) < 2:
        parser.error("Require at least two distinct query cases")
    import httpx

    slots = threading.BoundedSemaphore(4)
    reports = []
    with httpx.Client(
        base_url=args.url,
        headers={"Authorization": "Bearer " + args.key.read_text().strip()},
        timeout=15,
        trust_env=False,
    ) as client:

        def request(body, barrier=None):
            if barrier:
                barrier.wait(timeout=5)
            start = time.perf_counter()
            unique = list(dict.fromkeys(body["documents"]))
            batches = rerank_batches(body["query"], unique)

            def submit(documents):
                with slots:
                    reply = client.post("/v1/rerank", json=dict(body, documents=documents))
                    reply.raise_for_status()
                    return checked_values("rerank", reply.json(), len(documents))

            with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
                values = [v for row in pool.map(submit, batches) for v in row]
            mapping = dict(zip(unique, values))
            return (time.perf_counter() - start) * 1000, [mapping[d] for d in body["documents"]]

        for count in (20, 60):
            measurements, serial, concurrent, differences = [], [], [], []
            for repeat in range(args.repeats + 1):
                bodies = [
                    {"model": RERANK_MODEL, "query": item["query"], "documents": item["documents"][:count]}
                    for item in [cases[repeat % len(cases)], cases[(repeat + 1) % len(cases)]]
                ]
                observation, outputs = {}, {}
                for mode in (("serial", "concurrent") if repeat % 2 else ("concurrent", "serial")):
                    start = time.perf_counter()
                    if mode == "serial":
                        values = [request(body) for body in bodies]
                    else:
                        barrier = threading.Barrier(2)
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            values = list(pool.map(lambda body: request(body, barrier), bodies))
                    observation[mode] = {
                        "pair_ms": (time.perf_counter() - start) * 1000,
                        "request_ms": [v[0] for v in values],
                    }
                    outputs[mode] = [v[1] for v in values]
                difference = max(
                    abs(x - y) for a, b in zip(outputs["serial"], outputs["concurrent"]) for x, y in zip(a, b)
                )
                if difference > 0.0001:
                    args.output.write_text(
                        json.dumps(
                            {
                                "failed": "output_agreement",
                                "documents_per_request": count,
                                "repeat": repeat,
                                "case_indices": [repeat % len(cases), (repeat + 1) % len(cases)],
                                "max_output_difference": difference,
                                "outputs": outputs,
                            },
                            indent=2,
                        )
                        + "\n"
                    )
                    raise RuntimeError(
                        f"Batching changed request output beyond the numerical bound: {difference}"
                    )
                if repeat:
                    serial.append(observation["serial"]["pair_ms"])
                    concurrent.append(observation["concurrent"]["pair_ms"])
                    differences.append(difference)
                    measurements.append(observation)
            report = {
                "documents_per_request": count,
                **paired_summary(serial, concurrent),
                "max_output_difference": max(differences),
                "observations": measurements,
                "limits": "Six-query fixture; paired bootstrap is diagnostic, not a corpus-wide guarantee.",
            }
            reports.append(report)
            args.output.write_text(json.dumps(reports, indent=2) + "\n")
            print(json.dumps({k: v for k, v in report.items() if k != "observations"}), flush=True)


if __name__ == "__main__":
    main()
