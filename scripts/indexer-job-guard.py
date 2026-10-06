"""Record/verify active Job identity and spec hashes without saving credentials."""

import argparse
import hashlib
import json
from pathlib import Path
import sys


def snapshot(jobs):
    return {job["metadata"]["name"]: {"uid": job["metadata"]["uid"],
             "spec_sha256": hashlib.sha256(json.dumps(job["spec"], sort_keys=True).encode()).hexdigest()}
            for job in jobs["items"]
            if not any(condition.get("type") in ("Complete", "Failed") and condition.get("status") == "True"
                       for condition in job.get("status", {}).get("conditions", []))
            and not job["spec"].get("suspend", False)}


def verify(before, after):
    current = {job["metadata"]["name"]: job for job in after["items"]}
    for name, expected in before.items():
        job = current.get(name)
        if not job or job["metadata"]["uid"] != expected["uid"]:
            raise SystemExit(f"Existing Job identity changed or disappeared: {name}")
        checksum = hashlib.sha256(json.dumps(job["spec"], sort_keys=True).encode()).hexdigest()
        if checksum != expected["spec_sha256"]:
            raise SystemExit(f"Existing Job configuration changed: {name}")
    print(f"Verified {len(before)} pre-existing unfinished Job(s) were not changed")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--before", type=Path)
    args = cli.parse_args()
    jobs = json.load(sys.stdin)
    if args.before:
        verify(json.loads(args.before.read_text()), jobs)
    else:
        json.dump(snapshot(jobs), sys.stdout)
