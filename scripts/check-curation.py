"""Check the live LAN endpoint and rejection of non-LAN/spoofed clients."""

import argparse
import http.client
import ipaddress
import json
import os
import subprocess
import urllib.request


def ready_address(pods):
    """Use the ready host-network pod, ignoring old/draining addresses."""
    addresses = set()
    for pod in pods.get("items", []):
        status = pod.get("status", {})
        if (pod.get("metadata", {}).get("deletionTimestamp") or status.get("phase") != "Running"
                or not any(c.get("type") == "Ready" and c.get("status") == "True"
                           for c in status.get("conditions", []))):
            continue
        address = ipaddress.ip_address(status.get("hostIP", ""))
        if address not in ipaddress.ip_network("192.168.0.0/16"):
            raise ValueError("Ready curation pod must have a LAN host address")
        addresses.add(str(address))
    if len(addresses) != 1:
        raise ValueError("Expected one LAN address from ready curation pods")
    return addresses.pop()


def discover_address():
    data = subprocess.check_output([
        "sudo", "-n", "kubectl", "--kubeconfig=/etc/rancher/k3s/k3s.yaml",
        "-n", "eqarchives-es", "get", "pods", "-l", "app=eqarchives-curation", "-o", "json"
    ], timeout=15)
    return ready_address(json.loads(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--address", help="Override the ready curation pod's LAN host IP")
    args = parser.parse_args()
    address = args.address or discover_address()
    base = f"http://{address}:8090"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + "/healthz", timeout=15) as response:
        health = json.load(response)
    assert health["status"] == "ok"
    if os.environ.get("GITHUB_SHA"):
        assert health["version"] == os.environ["GITHUB_SHA"], "Curation build revision does not match this deployment"
    with opener.open(base + "/api/queue?compact=1&filter=queued", timeout=15) as response:
        queue = json.load(response)
        assert response.headers["Cache-Control"] == "no-store"
    assert isinstance(queue["all_count"], int) and isinstance(queue["candidates"], list)
    for path in ("/", "/api/queue", "/assets/review.js"):
        connection = http.client.HTTPConnection(address, 8090, source_address=("127.0.0.1", 0), timeout=15)
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
