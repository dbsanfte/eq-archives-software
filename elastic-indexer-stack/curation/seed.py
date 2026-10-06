"""Copy only a pilot database and its manifest-listed sources, once, into a PVC."""

import argparse
import json
import os
from pathlib import Path
import sqlite3

from common import CrawlError, capture_scope
from captures import LIMITS, verified_source
from state import connect


def seed(source, root, owner=None):
    source, root = Path(source), Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = root / "seed-complete"
    if marker.exists():
        return
    paths = [root]
    if (source / "crawl.sqlite3").is_file():
        if not (root / "crawl.sqlite3").exists():
            incoming = sqlite3.connect(f"file:{source / 'crawl.sqlite3'}?mode=ro", uri=True)
            database = sqlite3.connect(root / "crawl.sqlite3")
            try:
                incoming.backup(database)
            finally:
                incoming.close()
                database.close()
        with connect(root) as store:
            candidates = store.candidates()
            if len(candidates) > 50:
                raise CrawlError("Pilot seed exceeds the 50-candidate bound")
            captures = [capture for row in candidates for capture in json.loads(row["captures"])]
            if len(captures) > LIMITS["files"] or sum(item["bytes"] for item in captures) > LIMITS["bytes"]:
                raise CrawlError("Pilot seed exceeds the artifact bounds")
            for capture in captures:
                data = verified_source(source, capture)
                destination = root / capture["path"]
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.write_bytes(data)
                destination.chmod(0o600)
                paths.append(destination)
                parent = destination.parent
                while parent != root:
                    paths.append(parent)
                    parent = parent.parent
            for row in candidates:
                if not row["decision"]:
                    store.db.execute("UPDATE candidates SET scope=? WHERE id=?", (capture_scope(row["url"]), row["id"]))
            store.set("archive_repository", "/archive")
            store.db.commit()
    marker.touch(mode=0o600)
    paths.extend([marker, root / "crawl.sqlite3", root / "crawl.sqlite3-wal", root / "crawl.sqlite3-shm"])
    for path in set(paths):
        if path.exists():
            path.chmod(0o700 if path.is_dir() else 0o600)
            if owner is not None:
                os.chown(path, owner, owner)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--owner", type=int)
    args = parser.parse_args()
    seed(args.source, args.root, args.owner)
