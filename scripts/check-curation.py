"""Check the live LAN endpoint and rejection of non-LAN/spoofed clients."""

import argparse
import http.client
import json
import os
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--address", default="192.168.50.100")
    args = parser.parse_args()
    base = f"http://{args.address}:8090"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + "/healthz", timeout=15) as response:
        health = json.load(response)
    assert health["status"] == "ok"
    if os.environ.get("GITHUB_SHA"):
        assert health["version"] == os.environ["GITHUB_SHA"], "Curation build revision does not match this deployment"
    with opener.open(base + "/api/queue", timeout=15) as response:
        queue = json.load(response)
        assert response.headers["Cache-Control"] == "no-store"
    assert isinstance(queue["all_count"], int) and isinstance(queue["candidates"], list)
    for path in ("/", "/api/queue", "/assets/review.js"):
        connection = http.client.HTTPConnection(args.address, 8090, source_address=("127.0.0.1", 0), timeout=15)
        try:
            connection.request("GET", path, headers={"X-Forwarded-For": "192.168.50.20", "X-Real-IP": "192.168.50.20"})
            response = connection.getresponse()
            assert response.status == 403, "A non-LAN client bypassed the CIDR check"
            assert b"Intranet access only" in response.read()
        finally:
            connection.close()
    print(f"Verified {base}: {queue['all_count']} candidates, private UI/API/assets, build {health['version']}")


if __name__ == "__main__":
    main()
