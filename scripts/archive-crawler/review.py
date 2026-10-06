"""Offline source review and explicit decisions bound to immutable manifests."""

import json
from pathlib import Path

from common import CrawlError, digest, now, save
from grading import SIGNATURE, sources


def checked_sources(store, row):
    documents = sources(store, row, 10000000)
    rating = row["rating"]
    if rating and rating.get("signature") != digest({"grader": SIGNATURE, "documents": documents}):
        raise CrawlError("Source judgment is stale; grade and review again")
    return documents


def record(store, row):
    result = dict(row)
    for field in ("coverage", "evidence", "captures", "rating", "decision"):
        result[field] = json.loads(result[field]) if result[field] else None
    result["manifest_sha256"] = digest({key: result[key] for key in ("id", "url", "scope", "captures", "rating")})
    return result


def queue(store):
    rows = [record(store, row) for row in store.candidates()]
    def order(row):
        rating = row["rating"] or {}
        level = min((c["tier"] for c in row["captures"]), default=3)
        return (rating.get("grade", -1) < 2, level, -rating.get("grade", -1), -row["priority"], row["url"])
    rows.sort(key=order)
    return rows


def review(args, store):
    rows = queue(store)
    for row in rows:
        try:
            row["sources"] = checked_sources(store, row)
        except CrawlError as error:
            row["sources"] = []
            row["source_error"] = str(error)
    metadata = {"archive_sha": store.get("archive_sha"), "generated_at": now(),
                "discovery": store.get("discovery_result"), "transport": store.get("wayback_transport"),
                "grading": store.get("grading_result"), "budget_usd": store.get("luna_budget_usd")}
    save(store.root / "approval-queue.json", {"metadata": metadata, "candidates": rows})
    data = json.dumps({"metadata": metadata, "candidates": rows}, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = Path(__file__).with_name("review.html").read_text()
    output = store.root / "review.html"
    output.write_text(template.replace("/* QUEUE_DATA */", "const queue = " + data + ";"))
    output.chmod(0o600)
    print(f"Approval queue: {output}; {sum(row['state'] == 'approval_pending' for row in rows)} candidates awaiting review", flush=True)


def decisions(args, store):
    incoming = json.loads(Path(args.file).read_text())
    if not isinstance(incoming, list) or len(incoming) > 500:
        raise CrawlError("Expected a bounded list of review decisions")
    current = {r["id"]: r for r in queue(store)}
    validated = []
    for decision in incoming:
        if not isinstance(decision, dict) or set(decision) != {"id", "manifest_sha256", "decision"} or decision["decision"] not in ("approve", "reject", "defer"):
            raise CrawlError("Invalid review decision")
        row = current.get(decision["id"])
        if not row or row["manifest_sha256"] != decision["manifest_sha256"]:
            raise CrawlError("Review decision refers to changed or unknown staged evidence")
        if decision["decision"] == "approve":
            if not row["rating"] or not row["captures"]:
                raise CrawlError("Only graded, staged captures can be approved")
            checked_sources(store, row)  # Recheck artifacts and the judgment before approval.
        validated.append(decision)
    states = {"approve": "approved_waiting_batch", "reject": "rejected", "defer": "deferred"}
    for decision in validated:
        store.db.execute("UPDATE candidates SET decision=?,state=? WHERE id=?",
                         (json.dumps({**decision, "reviewed_at": now()}), states[decision["decision"]], decision["id"]))
    store.db.commit()
    store.event(None, "decisions_imported", {"count": len(validated)})
    print(f"Recorded {len(validated)} decisions; archive publication has not been invoked")


def batch(args, store):
    approved = []
    for row in queue(store):
        if row["state"] == "approved_waiting_batch":
            if row["decision"]["manifest_sha256"] != row["manifest_sha256"]:
                raise CrawlError("Approved manifest changed; review again")
            checked_sources(store, row)
            approved.append({key: row[key] for key in ("id", "url", "scope", "captures", "manifest_sha256", "decision")})
    save(store.root / "approved-batch.json", {"archive_sha": store.get("archive_sha"), "candidates": approved,
                                             "status": "awaiting_explicit_batch_publication"})
    print(f"Exported {len(approved)} approved candidates to {store.root / 'approved-batch.json'}")
