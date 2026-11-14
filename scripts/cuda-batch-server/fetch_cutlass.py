"""Fetch hash-pinned, build-only CUTLASS headers; never run downloaded code."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import urllib.request

COMMIT = "ad7b2f5e84fcfa124cb02b91d5bd26d238c0459e"
ARCHIVE_SHA256 = "36429479df2ab1cd1c7981c2d34e786106326ccfefc8846f902f76151a65e112"
URL = f"https://codeload.github.com/NVIDIA/cutlass/tar.gz/{COMMIT}"


def extract(archive, destination):
    total = 0
    with tarfile.open(archive) as source:
        for member in source:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != "cutlass-" + COMMIT
            ):
                raise ValueError("Unexpected archive path")
            relative = PurePosixPath(*path.parts[1:])
            if not relative.parts or (relative.parts[0] != "include" and str(relative) != "LICENSE.txt"):
                continue
            if not member.isfile() and not member.isdir():
                raise ValueError("Links and special files are forbidden")
            total += member.size
            if member.size > 8 * 1024 * 1024 or total > 256 * 1024 * 1024:
                raise ValueError("Header extraction exceeds bounds")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as stream:
                    target.write_bytes(stream.read(member.size))


def fetch(destination):
    if destination.exists():
        raise ValueError("Use a fresh destination for pinned headers")
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "cutlass.tar.gz"
        digest, size = hashlib.sha256(), 0
        with urllib.request.urlopen(URL, timeout=60) as reply, archive.open("wb") as output:
            while data := reply.read(1024 * 1024):
                size += len(data)
                if size > 64 * 1024 * 1024:
                    raise ValueError("Compressed archive exceeds bounds")
                digest.update(data)
                output.write(data)
        if digest.hexdigest() != ARCHIVE_SHA256:
            raise ValueError("CUTLASS archive checksum differs")
        extract(archive, destination)
    (destination / "source.json").write_text(
        json.dumps({"commit": COMMIT, "archive_sha256": ARCHIVE_SHA256}) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    fetch(parser.parse_args().destination)
