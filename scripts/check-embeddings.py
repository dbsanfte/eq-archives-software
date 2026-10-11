"""Verify the embedding API, optionally including a real vector-only search."""

import argparse
import json
import math
import os
import urllib.request


def post(url, payload, api_key=None):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env", help="Environment variable containing an upstream key")
    parser.add_argument("--search-url", help="Frontend Elasticsearch proxy URL for a KNN check")
    parser.add_argument("--check-batches", action="store_true", help="Compare near-limit batched inputs with individual embeddings")
    parser.add_argument("--batch-min-cosine", type=float, default=0.9999,
                        help="Minimum batch/serial cosine; CPU quantized kernels need 0.998")
    args = parser.parse_args()
    if not 0.99 <= args.batch_min_cosine <= 1:
        parser.error("batch-min-cosine must be between 0.99 and 1")
    key = os.environ[args.api_key_env] if args.api_key_env else None
    response = post(args.url, {
        "model": args.model,
        "input": ["search_query: ancient cyclops in ocean of tears"],
        "encoding_format": "float",
    }, key)
    assert response.get("model") == args.model, "Unexpected embedding model alias"
    assert len(response.get("data", [])) == 1, "Expected one embedding"
    vector = response["data"][0]["embedding"]
    assert isinstance(vector, list) and len(vector) == 768, "Expected 768 dimensions"
    assert all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector), "Invalid embedding values"
    norm = math.sqrt(sum(x*x for x in vector))
    assert abs(norm - 1) < 0.001, "Expected a unit-normalized embedding"
    print("Verified a finite, normalized 768-dimensional Nomic embedding.")
    if args.check_batches:
        # Each document is 480 tokens with this pinned tokenizer, including its
        # prefix and special tokens. More slots must not shrink that per-input limit.
        inputs = ["search_document: " + word * 474 for word in ("cat ", "dog ")]
        inputs.append("search_query: cleric complete heal")
        expected = [post(args.url, {"model": args.model, "input": [text], "encoding_format": "float"}, key)
                    ["data"][0]["embedding"] for text in inputs]
        batch = post(args.url, {"model": args.model, "input": inputs, "encoding_format": "float"}, key)
        assert batch.get("model") == args.model, "Unexpected batched model alias"
        rows = batch.get("data", [])
        assert len(rows) == len(inputs), "Missing or extra batch vectors"
        assert sorted(row["index"] for row in rows) == list(range(len(inputs))), "Invalid batch indices"
        for row in rows:
            actual = row["embedding"]
            reference = expected[row["index"]]
            assert len(actual) == len(reference) == 768, "Incorrect batched dimensions"
            assert all(math.isfinite(x) for x in actual + reference), "Invalid batched values"
            norms = [math.sqrt(sum(x*x for x in values)) for values in (actual, reference)]
            assert all(abs(norm - 1) < 0.001 for norm in norms), "Unnormalized batched vector"
            cosine = sum(x*y for x, y in zip(actual, reference)) / (norms[0] * norms[1])
            assert cosine > args.batch_min_cosine, "Batched vector differs from its individual input"
        assert sum(x*y for x, y in zip(expected[0], expected[1])) < 0.99, "Batch fixtures must be distinct"
        print("Verified near-limit batches, input/vector identity, odd final input, and serial compatibility.")
    if args.search_url:
        result = post(args.search_url, {
            "size": 1,
            "_source": False,
            "knn": {
                "field": "llm_summary_vector", "query_vector": vector,
                "k": 1, "num_candidates": 10,
            },
        })
        assert not result.get("error") and result.get("hits", {}).get("hits"), "Vector search returned no hits"
        print("Verified a vector-only search against the production index.")


if __name__ == "__main__":
    main()
