#!/usr/bin/env python3
"""Isolated fake Elasticsearch; pairs with the real CPU Nomic server in CI."""

import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json


DOCUMENT = {"_id": "test/ancient-cyclops.txt", "_source": {
    "id": "test/ancient-cyclops.txt", "title": "Ancient Cyclops",
    "url": "https://example.org/archive/ancient-cyclops",
    "text_full": "An archived EverQuest account about camping the ancient cyclops.",
    "capture_date": "1999-11-05T19:21:11+00:00",
}}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        assert self.path == "/eq-archive/_search"
        expected = "Basic " + base64.b64encode(b"ci-readonly:ci-password-never-a-real-secret").decode()
        assert self.headers.get("Authorization") == expected
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "ids" in body["query"]:
            hits = [DOCUMENT] if body["query"]["ids"]["values"] == [DOCUMENT["_id"]] else []
        else:
            # A missing/invalid real embedding must fail this end-to-end check.
            assert len(body["knn"]) == 3
            assert all(len(item["query_vector"]) == 768 for item in body["knn"])
            hits = [DOCUMENT]
        response = json.dumps({"hits": {"hits": hits}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9200), Fixture).serve_forever()
