"""Reject register spills in every compiled custom kernel, not only a sample."""

import argparse
from pathlib import Path
import re
import subprocess


def check(log, resources, sass):
    kernels = re.findall(r"Function properties for ([^\n]+)\s+([\s\S]*?)(?=ptxas|\Z)", log)
    if not kernels:
        raise ValueError("Missing ptxas kernel reports")
    for name, report in kernels:
        spills = re.findall(r"(\d+) bytes spill (?:stores|loads)", report)
        if len(spills) != 2 or any(int(n) for n in spills):
            raise ValueError(f"Register spill or incomplete report: {name}")
        stack = re.search(r"(\d+) bytes stack frame", report)
        if not stack or int(stack[1]):
            raise ValueError(f"Kernel uses a stack frame: {name}")
    functions = re.findall(r"Function ([^:]+):\s+([^\n]+)", resources)
    if not functions or {name.strip() for name, _ in functions} != {name.strip() for name, _ in kernels}:
        raise ValueError("Compiled kernel inventory differs from compiler report")
    for name, report in functions:
        local = re.search(r"\bLOCAL:(\d+)", report)
        if not local or int(local[1]):
            raise ValueError(f"Kernel uses local memory: {name}")
    if re.search(r"\b(?:LDL|STL)(?:\.|\s)", sass):
        raise ValueError("SASS contains local-memory loads/stores")
    if not re.search(r"\bHMMA\.", sass):
        raise ValueError("Expected SM86 Tensor Core instructions are absent")
    return {
        "kernels": len(kernels),
        "spill_load_bytes": 0,
        "spill_store_bytes": 0,
        "local_bytes": 0,
        "stack_bytes": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    resources = subprocess.check_output(["cuobjdump", "--dump-resource-usage", str(args.library)], text=True)
    sass = subprocess.check_output(["cuobjdump", "--dump-sass", str(args.library)], text=True)
    (args.output / "resources.txt").write_text(resources)
    (args.output / "sass.txt").write_text(sass)
    import json

    result = check(args.log.read_text(), resources, sass)
    (args.output / "spill-check.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
