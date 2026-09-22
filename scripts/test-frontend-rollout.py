#!/usr/bin/env python3
"""Exercise the manifest's shutdown hook while ingress endpoint removal is delayed."""

from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

import yaml


def check(manifest, routes, container, endpoint, replacement):
    deployment = next(r for r in yaml.safe_load_all(Path(manifest).read_text())
                      if r and r["kind"] == "Deployment" and r["metadata"]["name"] == "search-eqarchives")
    pod = deployment["spec"]["template"]["spec"]
    frontend = next(c for c in pod["containers"] if c["name"] == "search-eqarchives")
    hook = frontend.get("lifecycle", {}).get("preStop", {})
    command = hook["exec"]["command"] if hook else []
    grace = pod["terminationGracePeriodSeconds"]

    def retire():
        started = time.monotonic()
        if command:
            subprocess.run(["docker", "exec", container, *command], check=True, timeout=grace, stdout=subprocess.DEVNULL)
        remaining = max(1, math.ceil(grace - (time.monotonic() - started)))
        subprocess.run(["docker", "stop", "--time", str(remaining), container],
                       check=True, timeout=remaining + 5, stdout=subprocess.DEVNULL)

    def health():
        request = urllib.request.Request(endpoint + "/healthz", headers={"Host": "search.eqarchives.org"})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200 and response.read().strip() == b"ok"

    health()
    failed, observations = [], 0
    with ThreadPoolExecutor(max_workers=1) as pool:
        retiring = pool.submit(retire)
        start, switched = time.monotonic(), False
        while not retiring.done() or time.monotonic() - start < 4:
            # Model asynchronous EndpointSlice/ingress propagation after deletion.
            if not switched and time.monotonic() - start >= 1:
                path = Path(routes)
                config = json.loads(path.read_text())
                config["http"]["services"]["search-eqarchives"]["loadBalancer"]["servers"] = [{"url": replacement}]
                pending = path.with_suffix(".pending")
                pending.write_text(json.dumps(config))
                pending.replace(path)
                switched = True
            try:
                health()
            except Exception as error:
                failed.append(type(error).__name__)
            observations += 1
            time.sleep(0.05)
        retiring.result()
    health()
    assert not failed, f"Frontend dropped {len(failed)} of {observations} requests during endpoint handover: {failed[:5]}"
    print(f"Frontend shutdown regression passed: {observations} requests stayed healthy during delayed endpoint handover.")


if __name__ == "__main__":
    check(*sys.argv[1:])
