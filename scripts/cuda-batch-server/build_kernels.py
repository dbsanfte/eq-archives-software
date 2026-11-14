"""Compile measured SM86 profiles; every binary must pass the no-spill gate."""

import hashlib
import json
import os
from pathlib import Path
import subprocess

from check_kernels import check
from fetch_cutlass import COMMIT, ARCHIVE_SHA256

PROFILES = {
    "embed": {"TILE_M": 64, "TILE_N": 128, "TILE_K": 32, "ASYNC_COPY": 1, "MODEL_KIND": 2},
    "rerank": {
        "TILE_M": 64,
        "TILE_N": 128,
        "TILE_K": 32,
        "MODEL_KIND": 1,
        "USE_CUTLASS": 1,
        "CUTLASS_K": 32,
        "CUTLASS_STAGES": 3,
        "MIN_BLOCKS": 2,
    },
    "small": {"TILE_M": 32, "TILE_N": 32, "TILE_K": 64, "THREAD_WARPS": 4},
}


def main():
    root = Path(__file__).resolve().parent
    source = root / "layers.cu"
    cutlass = Path(os.environ.get("CUTLASS_ROOT", "/opt/cutlass"))
    if json.loads((cutlass / "source.json").read_text()) != {
        "commit": COMMIT,
        "archive_sha256": ARCHIVE_SHA256,
    }:
        raise ValueError("CUTLASS source pin differs")
    for name, defines in PROFILES.items():
        output = root / "compiler-report" / name
        output.mkdir(parents=True, exist_ok=True)
        library = root / f"lib{name}.so"
        command = [
            "nvcc",
            "-std=c++17",
            "-O3",
            "-lineinfo",
            "--expt-relaxed-constexpr",
            "-arch=sm_86",
            "-Xptxas=-v,-warn-spills",
            "-I" + str(cutlass / "include"),
            "--shared",
            "-Xcompiler=-fPIC",
            *[f"-D{k}={v}" for k, v in defines.items()],
            "-o",
            str(library),
            str(source),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        log = result.stdout + result.stderr
        (output / "ptxas.log").write_text(log)
        if result.returncode:
            print(log)
            result.check_returncode()
        resources = subprocess.check_output(["cuobjdump", "--dump-resource-usage", str(library)], text=True)
        sass = subprocess.check_output(["cuobjdump", "--dump-sass", str(library)], text=True)
        receipt = check(log, resources, sass)
        receipt.update(
            profile=name,
            defines=defines,
            cutlass_commit=COMMIT,
            cutlass_archive_sha256=ARCHIVE_SHA256,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            binary_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
        )
        (output / "resources.txt").write_text(resources)
        (output / "sass.txt").write_text(sass)
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))


if __name__ == "__main__":
    main()
