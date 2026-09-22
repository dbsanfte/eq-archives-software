#!/usr/bin/env python3
"""Isolated fake Elasticsearch; pairs with the real CPU Nomic server in CI."""

import base64
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from urllib.parse import urlsplit


DOCUMENTS = [{"_id": f"test/ancient-cyclops-{i:02d}.txt", "_source": {
    "id": f"test/ancient-cyclops-{i:02d}.txt", "title": f"Ancient Cyclops account {i}",
    "url": f"https://example.org/archive/ancient-cyclops-{i}",
    "text_full": "An archived EverQuest account about camping the ancient cyclops.",
    "domain_name": "alpha.example.org" if i < 12 else "beta.example.org",
    "mailing_list_name": "eq_wizards", "file_type": "text",
    "capture_date": "1999-11-05T19:21:11.000Z" if i < 12 else "2000-01-01T00:00:00.000Z",
    "llm_guessed_date": "1999-11-05",
}} for i in range(26)]


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        assert urlsplit(self.path).path == "/eq-archive/_search"
        expected = "Basic " + base64.b64encode(b"ci-readonly:ci-password-never-a-real-secret").decode()
        assert self.headers.get("Authorization") == expected
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        hits = list(DOCUMENTS)
        if "ids" in body["query"]:
            hits = [d for d in hits if d["_id"] in body["query"]["ids"]["values"]]
        elif "knn" in body:
            # A missing/invalid real embedding must fail the legacy hybrid check.
            assert len(body["knn"]) == 3
            assert all(len(item["query_vector"]) == 768 for item in body["knn"])
        else:
            assert "bool" in body["query"], "Legacy search unexpectedly lost its embeddings"
            for clause in body["query"]["bool"]["filter"]:
                if "terms" in clause:
                    field, values = next(iter(clause["terms"].items()))
                    hits = [d for d in hits if d["_source"].get(field) in values]
                else:
                    field, dates = next(iter(clause["range"].items()))
                    hits = [d for d in hits if field in d["_source"]
                            and dates.get("gte", "") <= d["_source"][field] <= dates.get("lte", "9999")]
            for sort in body.get("sort", [])[:1]:
                field, order = next(iter(sort.items()))
                if isinstance(order, dict):
                    hits.sort(key=lambda d: d["_source"].get(field, ""), reverse=order["order"] == "desc")
        result = {"hits": {"hits": [], "total": {"value": len(hits), "relation": "eq"}}}
        if "aggs" in body:
            composite = body["aggs"]["sources"]["composite"]
            field = composite["sources"][0]["value"]["terms"]["field"]
            counts = Counter(d["_source"].get(field) for d in hits if d["_source"].get(field))
            keys = sorted(k for k in counts if k > composite.get("after", {}).get("value", ""))
            keys = keys[:composite["size"]]
            aggregation = {"buckets": [{"key": {"value": k}, "doc_count": counts[k]} for k in keys]}
            if keys:
                aggregation["after_key"] = {"value": keys[-1]}
            result["aggregations"] = {"sources": aggregation}
        else:
            start = body.get("from", 0)
            result["hits"]["hits"] = hits[start:start + body["size"]]
        response = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9200), Fixture).serve_forever()
