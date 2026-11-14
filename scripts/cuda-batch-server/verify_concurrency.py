"""Bounded mixed-model GPU check, with an optional Nsight capture window."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import threading
import time

from backend import PREFIX, SUFFIX
from scheduler import Engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/models")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    engine = Engine(args.root)
    torch = engine.torch
    texts = {
        "embed": [
            f"title: Guide {i} | text: " + "A cleric heals the party in Lower Guk. " * 35 for i in range(8)
        ],
        "rerank": [
            PREFIX
            + f"How can a cleric heal a party?\n<Document>: Guide {i}. "
            + "A cleric heals the party in Lower Guk. " * 35
            + SUFFIX
            for i in range(8)
        ],
    }
    inputs = {kind: engine.encode(kind, values) for kind, values in texts.items()}
    reference = {kind: engine.run(kind, rows) for kind, rows in inputs.items()}
    sequential, concurrent, differences = [], [], []

    def run(kind, barrier):
        if barrier:
            barrier.wait(timeout=10)
        with torch.cuda.nvtx.range(kind):
            return kind, engine.run(kind, inputs[kind])

    if args.profile:
        torch.cuda.cudart().cudaProfilerStart()
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(5):
            start = time.perf_counter()
            for kind in inputs:
                run(kind, None)
            sequential.append((time.perf_counter() - start) * 1000)
            barrier = threading.Barrier(2)
            start = time.perf_counter()
            futures = [pool.submit(run, kind, barrier) for kind in inputs]
            results = dict(f.result(timeout=15) for f in futures)
            concurrent.append((time.perf_counter() - start) * 1000)
            for kind, values in results.items():
                error = (torch.tensor(values) - torch.tensor(reference[kind])).abs().max().item()
                differences.append(error)
                if error > 0.0001:
                    raise RuntimeError("Concurrent requests changed another model's output")
    if args.profile:
        torch.cuda.cudart().cudaProfilerStop()
    report = {
        "tokens": {kind: list(map(len, rows)) for kind, rows in inputs.items()},
        "sequential_ms": sequential,
        "concurrent_ms": concurrent,
        "speedup": statistics.median(sequential) / statistics.median(concurrent),
        "max_output_difference": max(differences),
        "gpu_peak_allocated": torch.cuda.max_memory_allocated(),
        "note": "Threads/streams alone do not prove overlap; inspect the Nsight kernel timeline.",
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
