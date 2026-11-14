"""Causal attention boundary/ownership regression, using the independent layer oracle.

Explicit operator command, suitable for compute-sanitizer. No network requests,
paid APIs or indexed writes. Model files and aggregate receipts stay private.
"""

import argparse
import json
from pathlib import Path

from backend import Model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/models")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oracle", action="store_true")
    parser.add_argument("--case", type=int, choices=(0, 1, 2), help="Select one bounded sanitizer case")
    args = parser.parse_args()
    import torch

    torch.set_num_threads(2)
    model = Model("rerank", args.root)
    if model.lib.layer_features() != 1:
        raise RuntimeError("This regression requires fused causal attention")
    words = model.tokenizer.encode("A cleric heals the party in Lower Guk. ", add_special_tokens=False).ids
    full = (words * 100)[:768]
    # Lengths straddle shared-prefix, key-tile and maximum-prompt boundaries.
    groups = [[full[:n] for n in lengths] for lengths in
              ((1, 31, 32, 33, 63, 64, 65), (127, 128, 129, 767, 768))]
    # Equal suffixes after different prefixes must not alias. Also exercise
    # duplicate rows, partially shared blocks and a longer non-owner sequence.
    other = list(full)
    other[32] = words[1]
    groups.append([full[:97], other[:97], full[:97], full[:65], other[:768]])
    reports = []
    for rows in (groups if args.case is None else [groups[args.case]]):
        if args.oracle:
            actual, layers = model.run(rows, compare_layers=True)
            error = max(layer["relative_l2"] for layer in layers)
            if error > 0.005:
                raise RuntimeError("Attention differs from independent per-layer reference")
        else:
            actual, error = model.run(rows), None
        separate = [model.run([row])[0] for row in rows]
        unshared = model.run(rows, share_prefix=False)
        reversed_values = model.run(list(reversed(rows)))[::-1]
        difference = max(abs(a - b) for values in (separate, unshared, reversed_values)
                         for a, b in zip(actual, values))
        if difference > 0.0001:
            raise RuntimeError("Attention changed with batch order, padding or prefix ownership")
        reports.append({"lengths": list(map(len, rows)), "max_layer_relative_l2": error,
                        "max_batch_difference": difference})
    args.output.write_text(json.dumps(reports, indent=2) + "\n")
    print(json.dumps(reports))


if __name__ == "__main__":
    main()
