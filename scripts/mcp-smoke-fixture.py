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
    "llm_guessed_date": "1999-11-05", "last_indexed": "2026-11-10T12:00:00.000Z",
    "mime_type": "text/plain", "llm_content_flavour": "quest", "llm_tags": ["cyclops", "quest"],
    "llm_model_name": "fixture-model",
}} for i in range(26)]

# Keep the legacy 26-document paging fixture, with real website identities for
# metadata directories/history. No production URLs or credentials are used.
for document in DOCUMENTS:
    source = document["_source"]
    path = document["_id"].split("/", 1)[1]
    source["id"] = document["_id"] = "websites/"+source["domain_name"]+"/19991105192111/"+path
    source["url"] = "https://web.archive.org/web/19991105192111/http://"+source["domain_name"]+"/"+path


def matches(clause, document):
    source = document["_source"]
    if "match_all" in clause or "simple_query_string" in clause:
        return True
    if "bool" in clause:
        node = clause["bool"]
        if not all(matches(c, document) for c in node.get("must", [])+node.get("filter", [])):
            return False
        if any(matches(c, document) for c in node.get("must_not", [])):
            return False
        return sum(matches(c, document) for c in node.get("should", [])) >= node.get("minimum_should_match", 0)
    if "match_phrase" in clause:
        field, value = next(iter(clause["match_phrase"].items()))
        return value in source.get(field, "")
    if "ids" in clause:
        return document["_id"] in clause["ids"]["values"]
    if "exists" in clause:
        return source.get(clause["exists"]["field"]) is not None
    if "regexp" in clause:
        import re
        field, value = next(iter(clause["regexp"].items()))
        return re.fullmatch(value["value"] if isinstance(value, dict) else value, source.get(field, "")) is not None
    for kind in ("term", "terms", "prefix", "range"):
        if kind not in clause:
            continue
        field, value = next(iter(clause[kind].items()))
        actual = source.get(field)
        if kind == "term":
            return actual == value
        if kind == "terms":
            return any(v in value for v in actual) if isinstance(actual, list) else actual in value
        if kind == "prefix":
            return isinstance(actual, str) and actual.startswith(value)
        return actual is not None and value.get("gte", "") <= actual <= value.get("lte", "9999")
    raise AssertionError("Unsupported fixture query")


def metrics(hits):
    def date_metric(field, maximum=False):
        values = [h["_source"][field] for h in hits if h["_source"].get(field)]
        return {"value_as_string": (max(values) if maximum else min(values))} if values else {"value": None}
    return {"doc_count": len(hits), "latest": date_metric("last_indexed", True),
            "first": date_metric("site_first_indexed_at"),
            "known_first": {"value": sum(bool(h["_source"].get("site_first_indexed_at")) for h in hits)},
            "earliest": date_metric("capture_date"), "newest": date_metric("capture_date", True)}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        assert urlsplit(self.path).path == "/eq-archive/_search"
        expected = "Basic " + base64.b64encode(b"ci-readonly:ci-password-never-a-real-secret").decode()
        assert self.headers.get("Authorization") == expected
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if 'sites_count' in body.get('aggs', {}):
            result = {'hits': {'hits': [], 'total': {'value': 26, 'relation': 'eq'}}, 'aggregations': {
                'sites_count': {'value': 2}, 'tagged': {'doc_count': 12},
                'themes': {'buckets': [{'key': 'quest', 'doc_count': 12, 'doc_count_error_upper_bound': 0}]},
                'sites': {'buckets': [{'key': 'alpha.example.org', 'doc_count': 12, 'doc_count_error_upper_bound': 0}]},
                'timeline': {'buckets': [{'key_as_string': '1999', 'doc_count': 26, 'sites': {'value': 2}}]},
            }}
            return self.reply(result)
        if 'sample' in body.get('aggs', {}):
            assert body['aggs']['sample']['aggs']['pages']['top_hits']['size'] <= 100
            return self.reply({'hits': {'hits': []}, 'aggregations': {'sample': {'pages': {'hits': {'hits': [{
                '_id': 'websites/alpha.example.org/19991105192111/cyclops.html',
                '_source': {'domain_name': 'alpha.example.org'},
                'fields': {'excerpt': ['An ancient cyclops wanders the desert. Platinum rings reward patient hunters.']},
            }]}}}}})
        hits = list(DOCUMENTS)
        if "ids" in body["query"]:
            ids = body["query"]["ids"]["values"]
            large = {"__mcp_admission_large__": 4 * 1024 * 1024 - 4096,
                     "__mcp_admission_oversized__": 4 * 1024 * 1024,
                     "__mcp_admission_browser__": 7 * 1024 * 1024}
            if len(ids) == 1 and ids[0] in large:
                source = {"id": ids[0], "title": "Synthetic admission source",
                          "url": "https://example.org/admission", "text_full": "x" * large[ids[0]]}
                return self.reply({"hits": {"hits": [{"_id": ids[0], "_source": source}]}})
            hits = [d for d in hits if d["_id"] in body["query"]["ids"]["values"]]
        elif "knn" in body:
            # A missing/invalid real embedding must fail the legacy hybrid check.
            assert len(body["knn"]) == 4
            assert any(branch["field"] == "image_vector" for branch in body["knn"])
            assert all(len(item["query_vector"]) == 768 for item in body["knn"])
            # The hybrid research leg must carry every exact restriction before
            # nearest-neighbour selection, plus the model guard when present.
            restrictions = body.get('query', {}).get('bool', {}).get('filter', [])
            if restrictions:
                assert all(branch.get('filter') in (restrictions,
                    restrictions+[{'term':{'embedding_model':'embeddinggemma-2-768-v1'}}]) for branch in body['knn'])
                hits = [d for d in hits if all(matches(c,d) for c in restrictions)]
        else:
            hits = [d for d in hits if matches(body["query"], d)]
            for sort in body.get("sort", [])[:1]:
                field, order = next(iter(sort.items()))
                if isinstance(order, dict):
                    hits.sort(key=lambda d: d["_source"].get(field, ""), reverse=order["order"] == "desc")
        result = {"hits": {"hits": [], "total": {"value": len(hits), "relation": "eq"}}}
        if "sites" in body.get("aggs", {}):
            domains = {d["_source"]["domain_name"] for d in hits}
            buckets = [{"key": domain, **metrics([d for d in hits if d["_source"]["domain_name"] == domain])}
                       for domain in sorted(domains)]
            buckets.sort(key=lambda b: b["latest"]["value_as_string"], reverse=True)
            result["aggregations"] = {"sites": {"buckets": buckets[:body["aggs"]["sites"]["terms"]["size"]]}}
        elif "details" in body.get("aggs", {}):
            result["aggregations"] = {"details": {"buckets": {domain: metrics([d for d in hits if matches(clause, d)])
                for domain, clause in body["aggs"]["details"]["filters"]["filters"].items()}}}
        elif "collections" in body.get("aggs", {}):
            result["aggregations"] = {"collections": {"buckets": {name: {"doc_count": sum(matches(clause,d) for d in hits)}
                for name, clause in body["aggs"]["collections"]["filters"]["filters"].items()}}}
        elif "aggs" in body:
            composite = body["aggs"]["sources"]["composite"]
            field = composite["sources"][0]["value"]["terms"]["field"]
            counts = Counter(value for d in hits for value in
                             (d["_source"].get(field) if isinstance(d["_source"].get(field), list)
                              else [d["_source"].get(field)]) if value)
            keys = sorted(k for k in counts if k > composite.get("after", {}).get("value", ""))
            keys = keys[:composite["size"]]
            aggregation = {"buckets": [{"key": {"value": k}, "doc_count": counts[k]} for k in keys]}
            if keys:
                aggregation["after_key"] = {"value": keys[-1]}
            result["aggregations"] = {"sources": aggregation}
        else:
            start = body.get("from", 0)
            result["hits"]["hits"] = hits[start:start + body["size"]]
        self.reply(result)

    def reply(self, result):
        response = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9200), Fixture).serve_forever()
