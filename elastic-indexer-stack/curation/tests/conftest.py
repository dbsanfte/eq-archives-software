import json
from pathlib import Path
import sys

import pytest

SOFTWARE = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(SOFTWARE / "scripts/archive-crawler"), str(Path(__file__).resolve().parents[1]),
                str(SOFTWARE / "elastic-indexer-stack/indexer/src")]

from common import capture_scope, digest
from captures import archive_path
from grading import SIGNATURE, sources
from review import record
from state import connect


def add_candidate(root, url="http://guild.example/eq/news.html", body=None, identifier=None, grade=3):
    identifier = identifier or digest(url)[:24]
    body = body or b'<title>EQ Guild</title><p>EverQuest guild history.</p><a href="guide.html">Guide</a>'
    with connect(root) as store:
        path = store.root / "captures" / identifier / "20000101000000.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        capture = {"url": url, "timestamp": "20000101000000", "requested_timestamp": "20000101000000",
                   "timestamp_basis": "memento_datetime", "path": str(path.relative_to(root)), "bytes": len(body),
                   "sha256": digest(body), "tier": 1, "source": "wayback", "title": "EQ Guild"}
        store.db.execute("INSERT INTO candidates(id,url,scope,priority,coverage,evidence,captures,state) VALUES (?,?,?,?,?,?,?,'sampled')",
                         (identifier, url, capture_scope(url), 100, '{"status":"absent_host"}', '[]', json.dumps([capture])))
        row = store.db.execute("SELECT * FROM candidates WHERE id=?", (identifier,)).fetchone()
        if grade is not None:
            rating = {"grade": grade, "category": "guild", "confidence": "high", "reason": "A contemporary EQ guild.",
                      "evidence": [{"slot": 0, "excerpt": "EverQuest guild history."}], "origin": "model",
                      "signature": digest({"grader": SIGNATURE, "documents": sources(store, row, 120000)})}
            store.db.execute("UPDATE candidates SET rating=?,state='approval_pending' WHERE id=?", (json.dumps(rating), identifier))
        store.db.commit()
        return record(store, store.db.execute("SELECT * FROM candidates WHERE id=?", (identifier,)).fetchone())


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "state"
    return root, add_candidate(root)


def manifest_for(row, batch_id="a" * 32):
    capture = row["captures"][0]
    return {"schema": 1, "batch_id": batch_id,
            "sites": [{key: row[key] for key in ("id", "url", "scope", "scope_mode", "captures", "manifest_sha256")}],
            "captures": [{**capture, "candidate_id": row["id"], "archive_path": archive_path(capture)}]}
