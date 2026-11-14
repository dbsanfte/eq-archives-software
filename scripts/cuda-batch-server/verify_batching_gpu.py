"""Check merged query batches against separate forwards on private real passages."""

import argparse
import json
from pathlib import Path
import time

from backend import Model, PREFIX, SUFFIX, packed_layout
from benchmark_batching import paired_summary
from benchmark_http import load_cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("/models"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-request", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--oracle", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.per_request <= 16 or not 2 <= args.repeats <= 20:
        parser.error("Require 1–16 documents per request and 2–20 repetitions")
    import torch

    torch.set_num_threads(2)
    cases = load_cases(args.fixtures, args.root)
    if len(cases) < 2:
        parser.error("Require two distinct query cases")
    model, reports = Model("rerank", args.root), []
    # Short inputs previously switched from WMMA to CUTLASS when another request
    # joined them. Check those real fixture prompts singly and together as well.
    short = []
    for item in cases:
        for doc in item["documents"]:
            row = model.encode([PREFIX + item["query"] + "\n<Document>: " + doc + SUFFIX])[0]
            if len(row) <= 128 and row not in short:
                short.append(row)
    for start in range(0, len(short), 16):
        rows = short[start : start + 16]
        separate = [model.run([row])[0] for row in rows]
        together = model.run(rows)
        if max(abs(x - y) for x, y in zip(separate, together)) > 0.0001:
            raise RuntimeError("Short input score changed when another request joined its batch")
    print(json.dumps({"short_prompts_checked": len(short)}), flush=True)
    for i in range(0, len(cases) - 1, 2):
        inputs = [
            model.encode(
                [
                    PREFIX + item["query"] + "\n<Document>: " + doc + SUFFIX
                    for doc in item["documents"][: args.per_request]
                ]
            )
            for item in cases[i : i + 2]
        ]
        merged = inputs[0] + inputs[1]
        separate_size = sum(len(packed_layout(rows, share_prefix=True).ids) for rows in inputs)
        merged_size = len(packed_layout(merged, share_prefix=True).ids)
        if merged_size > separate_size:
            raise RuntimeError("Merging queries discarded reusable prefix work")
        baseline = model.run(inputs[0]) + model.run(inputs[1])
        if args.oracle:
            actual, layers = model.run(merged, compare_layers=True)
            if max(row["relative_l2"] for row in layers) > 0.005:
                raise RuntimeError("Merged query layer comparison failed")
        else:
            actual = model.run(merged)
        difference = max(abs(x - y) for x, y in zip(actual, baseline))
        if len(actual) != len(baseline) or difference > 0.0001:
            raise RuntimeError("Merged query batch changed an output")
        times = {"serial": [], "batched": []}
        for repeat in range(args.repeats):
            for mode in (("serial", "batched") if repeat % 2 else ("batched", "serial")):
                start = time.perf_counter()
                if mode == "serial":
                    model.run(inputs[0])
                    model.run(inputs[1])
                else:
                    model.run(merged)
                times[mode].append((time.perf_counter() - start) * 1000)
        report = {
            "cases": [i, i + 1],
            "rows": list(map(len, inputs)),
            "separate_packed_tokens": separate_size,
            "merged_packed_tokens": merged_size,
            "max_output_difference": difference,
            **paired_summary(times["serial"], times["batched"]),
        }
        reports.append(report)
        args.output.write_text(json.dumps(reports, indent=2) + "\n")
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
