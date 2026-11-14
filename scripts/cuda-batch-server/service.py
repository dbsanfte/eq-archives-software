"""Additive GPU service. Existing llama.cpp services and Kubernetes Jobs are untouched."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

ROOT = Path("/opt/eqarchives-cuda-batch-service")
STATE = Path("/var/lib/eqarchives-cuda-batch")
CONFIG = Path("/etc/eqarchives-cuda-batch/config.json")
NAME = "eqarchives-cuda-batch"
LABEL = "eqarchives.managed=cuda-batch"


def settings(path=CONFIG):
    result = json.loads(path.read_text())
    if set(result) != {"image", "bind_address", "revision"}:
        raise ValueError("Unexpected service settings")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", result["image"]):
        raise ValueError("An immutable local image ID is required")
    if not re.fullmatch(r"[0-9a-f]{40}", result["revision"]):
        raise ValueError("An exact source revision is required")
    if ipaddress.ip_address(result["bind_address"]) not in ipaddress.ip_network("192.168.0.0/16"):
        raise ValueError("The service must bind to the local intranet")
    return result


def command(config):
    return [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--name",
        NAME,
        "--label",
        LABEL,
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--cpus",
        "4",
        "--memory",
        "6g",
        "--pids-limit",
        "256",
        "--device",
        "nvidia.com/gpu=all",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--env",
        "CUDA_CACHE_PATH=/tmp/cuda",
        "--env",
        "GPU_BATCH_SIZE=32",
        "--publish",
        "127.0.0.1:8084:8080",
        "--publish",
        config["bind_address"] + ":8084:8080",
        "--mount",
        f"type=bind,source={STATE}/models,target=/models,readonly",
        "--mount",
        "type=bind,source=/etc/eqarchives-embeddings/openai_api_key,target=/run/secrets/embed_key,readonly",
        "--mount",
        "type=bind,source=/etc/eqarchives-reranker/api_key,target=/run/secrets/rerank_key,readonly",
        config["image"],
    ]


def health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8084/health", timeout=5) as reply:
            return json.load(reply).get("status") == "ok"
    except Exception:
        return False


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("start", "health", "watch"))
    args = p.parse_args()
    if args.action in ("health", "watch"):
        healthy = health()
        if args.action == "watch":
            counter = STATE / "health-failures"
            failures = 0 if healthy else (int(counter.read_text()) if counter.exists() else 0) + 1
            counter.write_text(str(failures))
            if failures >= 3:
                subprocess.run(["systemctl", "restart", NAME + ".service"], check=True)
                counter.write_text("0")
        print("CUDA batch service healthy" if healthy else "CUDA batch service unavailable")
        return 0 if healthy else 1
    config = settings()
    inspect = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", NAME], capture_output=True, text=True
    )
    if inspect.returncode == 0:
        labels = json.loads(inspect.stdout)
        if not isinstance(labels, dict) or labels.get("eqarchives.managed") != "cuda-batch":
            raise ValueError("Refusing to replace an unrelated container")
        subprocess.run(["docker", "rm", "-f", NAME], check=True, stdout=subprocess.DEVNULL)
    os.execvp("docker", command(config))


if __name__ == "__main__":
    raise SystemExit(main())
