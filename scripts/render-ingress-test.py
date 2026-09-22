#!/usr/bin/env python3
"""Render production ingress matches/priorities for an isolated Traefik routing test.

The file provider exercises the same rule matcher and priority ordering without
requiring a Kubernetes cluster. Backend names resolve on the Docker test network.
TLS/certificates/middleware remain covered by production deployment checks.
"""

import json
from pathlib import Path
import sys

import yaml


def render(paths):
    resources = [resource for path in paths for resource in yaml.safe_load_all(Path(path).read_text()) if resource]
    services = {r["metadata"]["name"]: r for r in resources if r["kind"] == "Service"}
    config = {"http": {"routers": {}, "services": {}}}
    for resource in resources:
        if resource["kind"] != "Ingress":
            continue
        metadata = resource["metadata"]
        annotations = metadata.get("annotations", {})
        for rule in resource["spec"]["rules"]:
            if rule["host"] != "search.eqarchives.org":
                continue
            for index, path in enumerate(rule["http"]["paths"]):
                backend = path["backend"]["service"]
                name = backend["name"]
                port = backend["port"].get("number")
                if port is None:
                    port = next(p["port"] for p in services[name]["spec"]["ports"] if p.get("name") == backend["port"]["name"])
                matcher = "Path" if path["pathType"] == "Exact" else "PathPrefix"
                matcher = annotations.get("traefik.ingress.kubernetes.io/router.pathmatcher", matcher)
                config["http"]["routers"][f'{metadata["name"]}-{index}'] = {
                    "rule": f'Host(`{rule["host"]}`) && {matcher}(`{path["path"]}`)',
                    "priority": int(annotations.get("traefik.ingress.kubernetes.io/router.priority", "0")),
                    "entryPoints": ["web"], "service": name,
                }
                config["http"]["services"][name] = {"loadBalancer": {"servers": [{"url": f"http://{name}:{port}"}]}}
    assert len(config["http"]["routers"]) >= 2, "Expected overlapping frontend and MCP routes"
    return config


if __name__ == "__main__":
    json.dump(render(sys.argv[1:]), sys.stdout)
