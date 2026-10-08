"""Offline source review and explicit decisions bound to immutable manifests."""

import json
from datetime import datetime, timedelta

from pathlib import Path

from capture_flow import Action, transition
from common import CrawlError, digest, now, save, original_url, within_capture_scope
from grading import judgment_signature, sources


def checked_sources(store, row):
    documents = sources(store, row, 10000000)
    rating = row["rating"]
    if rating and rating.get("signature") != judgment_signature(documents, rating.get('grading_criteria', '')):
        raise CrawlError("Source judgment is stale; grade and review again")
    return documents


def record(store, row):
    result = dict(row)
    for field in ("coverage", "evidence", "captures", "rating", "decision"):
        result[field] = json.loads(result[field]) if result[field] else None
    result["scope_mode"] = (result["coverage"] or {}).get("scope_mode", "directory")
    result['scope_has_source'] = result['scope_mode'] in ('custom', 'ezboard', 'sitepowerup') or any(
        original_url(capture['url']) == original_url(result['url']) if result['scope_mode'] == 'page'
        else within_capture_scope(capture['url'], result['scope']) for capture in result['captures'])
    from ezboard import board_url, board_name
    result['ezboard'] = board_name(result['url']) if board_url(result['url']) else None
    from sitepowerup import address
    board = address(result['url'])
    result['sitepowerup'] = board['board'] if board else None
    result["manifest_sha256"] = digest({key: result[key] for key in ("id", "url", "scope", "scope_mode", "captures", "rating")})
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
    apply_decisions(store, incoming)
    print(f"Recorded {len(incoming)} decisions; archive publication has not been invoked")


def apply_decisions(store, incoming, capture_delay=0, before_approve=None):
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        _apply_decisions(store, incoming, capture_delay, before_approve)
    store.event(None, "decisions_imported", {"count": len(incoming)})


def _apply_decisions(store, incoming, capture_delay=0, before_approve=None):
    if not isinstance(incoming, list) or len(incoming) > 500:
        raise CrawlError("Expected a bounded list of review decisions")
    current = {r["id"]: r for r in queue(store)}
    validated, seen = [], set()
    for decision in incoming:
        if not isinstance(decision, dict) or set(decision) != {"id", "manifest_sha256", "decision"} or decision["decision"] not in ("approve", "reject", "defer"):
            raise CrawlError("Invalid review decision")
        if not isinstance(decision["id"], str) or not isinstance(decision["manifest_sha256"], str) or decision["id"] in seen:
            raise CrawlError("Invalid or duplicate candidate ID")
        seen.add(decision["id"])
        row = current.get(decision["id"])
        if not row or row["manifest_sha256"] != decision["manifest_sha256"]:
            raise CrawlError("Review decision refers to changed or unknown staged evidence")
        if (row.get('coverage') or {}).get('capture', {}).get('continuation'):
            raise CrawlError('Undo queued regeneration before changing its capture decision')
        if row["state"] in ("already_archived", "duplicate_candidate") or row['state'] == 'coverage_unverified' and decision['decision'] == 'approve':
            raise CrawlError("Candidate is already archived, duplicated or its coverage is unverified")
        if row["state"] in ("capturing", "captured_awaiting_review", "publication_requested", "published", "indexed"):
            raise CrawlError("Candidate already belongs to a capture batch")
        if decision["decision"] == "approve":
            if before_approve:
                before_approve(store, row)
            if not row["rating"] or not row["captures"]:
                raise CrawlError("Only graded, staged captures can be approved")
            if not row['scope_has_source']:
                raise CrawlError('The saved scope excludes its graded source. Choose a scope containing the source before approving.')
            checked_sources(store, row)  # Recheck artifacts and the judgment before approval.
        validated.append((decision, transition(row['state'], Action(decision['decision']))))
    for decision, state in validated:
        reviewed_at = now()
        grant = {**decision, 'reviewed_at': reviewed_at}
        if decision['decision'] == 'approve' and capture_delay:
            grant['capture_after'] = (datetime.fromisoformat(reviewed_at) + timedelta(seconds=capture_delay)).isoformat()
        store.db.execute("UPDATE candidates SET decision=?,state=? WHERE id=?",
                         (json.dumps(grant), state, decision['id']))


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
