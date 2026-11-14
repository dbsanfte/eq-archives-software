"""Explicit, bounded GPU validation; never writes indexed data or calls paid APIs."""

import argparse
import json
import statistics
import time
from pathlib import Path

from backend import Model, PREFIX, SUFFIX, EMBED_MODEL, RERANK_MODEL


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kind", choices=("embed", "rerank"), required=True)
    p.add_argument("--root", default="/models")
    p.add_argument("--library")
    p.add_argument("--output", required=True)
    p.add_argument("--batch", type=int, default=2)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument(
        "--length", type=int, help="Approximate token length for bounded synthetic stress/profiling"
    )
    p.add_argument("--profile", action="store_true")
    p.add_argument("--oracle", action="store_true")
    p.add_argument("--llama-url")
    p.add_argument("--key-file")
    args = p.parse_args()
    if not 1 <= args.repeats <= 20:
        p.error("Use 1–20 bounded timing repetitions")
    import torch

    torch.set_num_threads(2)
    model = Model(args.kind, args.root, args.library)
    if args.kind == "embed":
        inputs = [
            "task: search result | query: Where can a cleric find the Water Sprinkler of Nem Ankh?",
            "title: none | text: The cleric epic quest requires the Orb of Frozen Water and the Orb of Clear Water. "
            * 6,
        ]
        inputs = [inputs[i % 2] for i in range(args.batch)]
    else:
        query = "How does a cleric obtain the epic weapon?"
        docs = [
            "A cleric obtains the Water Sprinkler of Nem Ankh by completing the epic quest. " * 6,
            "Sourdough bread uses flour, water and salt. " * 6,
        ]
        docs = [docs[i % 2] for i in range(args.batch)]
        inputs = [PREFIX + query + "\n<Document>: " + doc + SUFFIX for doc in docs]
    if args.length is not None:
        limit = 512 if args.kind == "embed" else 768
        if not 128 <= args.length <= limit:
            p.error(f"Stress length must be 128–{limit}")
        prefix = "title: none | text: " if args.kind == "embed" else PREFIX + query + "\n<Document>: "
        suffix = "" if args.kind == "embed" else SUFFIX
        sample = model.tokenizer.encode(
            "The cleric epic quest requires the Orb of Frozen Water. " * 100, add_special_tokens=False
        ).ids
        inputs, docs = [], []
        for i in range(args.batch):
            label = f"Document {i}: "
            allowance = args.length - len(
                model.tokenizer.encode(prefix + label + suffix, add_special_tokens=model.gemma).ids
            )
            doc = label + model.tokenizer.decode(sample[:allowance])
            while (
                len(model.tokenizer.encode(prefix + doc + suffix, add_special_tokens=model.gemma).ids)
                > args.length
            ):
                allowance -= 1
                doc = label + model.tokenizer.decode(sample[:allowance])
            inputs.append(prefix + doc + suffix)
            docs.append(doc)
    tokens = model.encode(inputs)
    result = {"kind": args.kind, "batch": len(tokens), "tokens": list(map(len, tokens))}
    if args.oracle:
        output, layers = model.run(tokens, compare_layers=True)
        result["layers"] = layers
        result["max_layer_relative_l2"] = max(r["relative_l2"] for r in layers)
        if result["max_layer_relative_l2"] > 0.005:
            raise RuntimeError("Layer comparison failed")
        if args.kind == "rerank":
            separate = model.run(tokens, share_prefix=False)
            result["prefix_sharing_max_error"] = max(abs(a - b) for a, b in zip(output, separate))
            if result["prefix_sharing_max_error"] > 0.0001:
                raise RuntimeError("Shared causal prefix changed an output")
    else:
        output = model.run(tokens)
    if args.llama_url:
        import httpx

        body = (
            {"model": EMBED_MODEL, "input": inputs}
            if args.kind == "embed"
            else {"model": RERANK_MODEL, "query": query, "documents": docs}
        )
        headers = {"Authorization": "Bearer " + Path(args.key_file).read_text().strip()}
        with httpx.Client(trust_env=False, timeout=60, headers=headers) as client:
            start = time.perf_counter()
            response = client.post(args.llama_url, json=body)
            response.raise_for_status()
            result["llama_ms"] = (time.perf_counter() - start) * 1000
            data = response.json()
        if args.kind == "embed":
            expected = [r["embedding"] for r in sorted(data["data"], key=lambda r: r["index"])]
            result["cosines"] = torch.nn.functional.cosine_similarity(
                torch.tensor(output), torch.tensor(expected)
            ).tolist()
            if min(result["cosines"]) < 0.999:
                raise RuntimeError("Embedding compatibility failed")
        else:
            expected = [r["relevance_score"] for r in sorted(data["results"], key=lambda r: r["index"])]
            result["scores"], result["llama_scores"] = output, expected
            result["max_abs_score_error"] = max(abs(a - b) for a, b in zip(output, expected))
            if result["max_abs_score_error"] > 0.03:
                raise RuntimeError("Reranker compatibility failed")
    times = []
    if args.profile:
        torch.cuda.cudart().cudaProfilerStart()
    for _ in range(args.repeats):
        start = time.perf_counter()
        model.run(tokens)
        times.append((time.perf_counter() - start) * 1000)
    if args.profile:
        torch.cuda.cudart().cudaProfilerStop()
    result["native_ms"] = times
    result["native_median_ms"] = statistics.median(times)
    result["cooperative_blocks"] = model.blocks
    result["gpu_peak_allocated"] = torch.cuda.max_memory_allocated()
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "layers"}))


if __name__ == "__main__":
    main()
