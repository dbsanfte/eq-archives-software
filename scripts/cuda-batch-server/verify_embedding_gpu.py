"""Bounded Gemma regression for the small profile and its batching boundary.

Explicit offline GPU command. Reports contain aggregate diagnostics; no model
outputs, archive writes or paid requests. The independent oracle checks all
layers, while an optional prior small library checks native runtime regression.
"""

import argparse
import json
import math
from pathlib import Path

from backend import Model, load_library


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/models")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oracle", action="store_true")
    parser.add_argument("--baseline-library", help="Optional previous libsmall.so")
    parser.add_argument("--case", type=int, help="Select one bounded sanitizer case")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    import torch

    torch.set_num_threads(2)
    model = Model("embed", args.root)
    previous = load_library(args.baseline_library) if args.baseline_library else None
    sample = model.tokenizer.encode(
        "title: none | text: A cleric heals the party in Lower Guk. ",
        add_special_tokens=True,
    ).ids
    full = (sample * 100)[:512]
    # Total padded size <=128 selects the small profile. Include ragged batches,
    # duplicates, every tile boundary and transitions to the bulk profile.
    lengths = [(1, 33), (31, 31, 31, 31), (32, 33, 64), (127, 128)]
    lengths += [(n,) for n in (1, 31, 32, 33, 63, 64, 65, 95, 96, 97, 127, 128, 129, 511, 512)]
    if args.case is not None and not 0 <= args.case < len(lengths):
        parser.error("Case is outside the bounded fixture")
    selected = lengths if args.case is None else [lengths[args.case]]
    reports = []
    for sizes in selected:
        rows = [full[:n] for n in sizes]
        if args.oracle:
            actual, layers = model.run(rows, compare_layers=True)
            error = max(layer["relative_l2"] for layer in layers)
            if not math.isfinite(error) or error > 0.005:
                raise RuntimeError("Gemma differs from independent per-layer reference")
        else:
            actual, error = model.run(rows), None
        actual = torch.tensor(actual)
        if (actual.shape != (len(rows), 768) or not torch.isfinite(actual).all()
                or (actual.norm(dim=1) - 1).abs().max() > 0.002):
            raise RuntimeError("Invalid embedding identity, shape or normalization")
        comparisons = {
            "separate": [model.run([row])[0] for row in rows],
            "reversed": model.run(list(reversed(rows)))[::-1],
        }
        if previous is not None:
            current, model.small = model.small, previous
            try:
                comparisons["previous"] = model.run(rows)
            finally:
                model.small = current
        differences = {}
        for name, values in comparisons.items():
            values = torch.tensor(values)
            cosine = torch.nn.functional.cosine_similarity(actual, values).min().item()
            difference = (actual - values).abs().max().item()
            if not math.isfinite(cosine) or cosine < 0.9999 or difference > 0.002:
                raise RuntimeError("Embedding changed with batching or prior native runtime")
            differences[name] = {"minimum_cosine": cosine, "max_abs_error": difference}
        if args.profile:
            # Only serving forwards belong in the trace; exclude the oracle.
            torch.cuda.cudart().cudaProfilerStart()
            model.run(rows)
            torch.cuda.cudart().cudaProfilerStop()
        reports.append({"lengths": list(sizes), "max_layer_relative_l2": error,
                        "comparisons": differences})
    args.output.write_text(json.dumps(reports, indent=2) + "\n")
    print(json.dumps(reports))


if __name__ == "__main__":
    main()
