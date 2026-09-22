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
    args = parser.parse_args()
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
