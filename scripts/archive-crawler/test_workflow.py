"""Paid grading and approval behavior with complete staged fixture sources."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from common import CrawlError, Store, digest
from acquisition import Downloader, sample
from grading import SIGNATURE, grade, reserve, sources, validate
from review import batch, decisions, queue, review


def response(excerpt="EverQuest guild history", status="completed"):
    rating = {"grade": 3, "category": "guild", "confidence": "high", "reason": "Contemporary guild history.",
              "evidence": [{"slot": 0, "excerpt": excerpt}]}
    return {"status": status, "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(rating)}]}],
            "usage": {"input_tokens": 500, "output_tokens": 150}}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def candidate(self, identifier="guild", text="EverQuest guild history"):
        destination = self.store.root / (identifier + ".html")
        data = ("<title>EQ Guild</title><p>" + text + "</p>").encode()
        destination.write_bytes(data)
        capture = {"path": destination.name, "url": "http://guild.example/", "timestamp": "20000101000000", "tier": 1,
                   "sha256": digest(data), "bytes": len(data), "site_coverage": "bounded_samples"}
        self.store.db.execute("INSERT INTO candidates(id,url,scope,priority,coverage,evidence,captures) VALUES (?,?,?,?,?,?,?)",
                              (identifier, "http://" + identifier + ".example/", "http://guild.example/", 100,
                               json.dumps({"status": "absent_host"}), "[]", json.dumps([capture])))
        self.store.db.commit()
        return self.store.db.execute("SELECT * FROM candidates WHERE id=?", (identifier,)).fetchone()

    def arguments(self, maximum=2):
        return SimpleNamespace(api_key_file="unused-private-fixture", max_usd=maximum, max_candidates=50, max_source_characters=120000)

    def grade(self, result=None, maximum=2):
        with patch("grading.Luna") as client:
            client.return_value.request.return_value = result or response()
            grade(self.arguments(maximum), self.store)
            return client.return_value.request.call_count

    def decision_file(self, rows):
        path = self.root / "decisions.json"
        path.write_text(json.dumps(rows))
        return SimpleNamespace(file=str(path))

    def test_completed_source_bound_grade_is_cached_without_second_payment(self):
        self.candidate()
        self.assertEqual(self.grade(), 1)
        row = queue(self.store)[0]
        self.assertEqual(row["state"], "approval_pending")
        self.assertEqual(row["rating"]["origin"], "model")
        self.assertEqual(self.grade(), 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        self.assertGreater(self.store.get("grading_result")["estimated_usd"], 0)

    def test_budget_reserved_before_call_and_survives_unknown_failure(self):
        self.candidate()
        self.assertEqual(self.grade(maximum=0.00001), 0)
        with patch("grading.Luna") as client:
            client.return_value.request.side_effect = CrawlError("ambiguous request failure")
            grade(self.arguments(), self.store)
        reserved = self.store.db.execute("SELECT SUM(reserved) FROM attempts").fetchone()[0]
        self.assertGreater(reserved, 0)
        self.assertEqual(self.grade(maximum=reserved), 0)
        for invalid in (float("nan"), float("inf"), -1):
            with self.assertRaises(CrawlError):
                reserve(self.store, "guild", "test", {"max_output_tokens": 2500}, invalid)

    def test_incomplete_or_unfounded_response_does_not_enter_approval_queue(self):
        row = self.candidate()
        documents = sources(self.store, row, 120000)
        for result in (response(status="incomplete"), response(excerpt="invented unrelated quotation")):
            with self.assertRaises(CrawlError):
                validate(result, documents)
        self.grade(response(status="incomplete"))
        self.assertIsNone(queue(self.store)[0]["rating"])
        self.assertEqual(queue(self.store)[0]["state"], "grade_error")

    def test_full_source_limits_and_tampering_leave_unjudged(self):
        row = self.candidate()
        with self.assertRaises(CrawlError):
            sources(self.store, row, 5)
        self.grade()
        (self.store.root / "guild.html").write_text("changed source")
        self.assertEqual(self.grade(), 0)
        self.assertIsNone(queue(self.store)[0]["rating"])

    def test_explicit_approval_and_batch_export_only_selected_captures(self):
        self.candidate()
        self.candidate("second")
        self.grade()
        rows = queue(self.store)
        selected = {key: rows[0][key] for key in ("id", "manifest_sha256")}
        decisions(self.decision_file([{**selected, "decision": "approve"}]), self.store)
        batch(SimpleNamespace(), self.store)
        exported = json.loads((self.store.root / "approved-batch.json").read_text())
        self.assertEqual([r["id"] for r in exported["candidates"]], [selected["id"]])
        self.assertEqual(exported["status"], "awaiting_explicit_batch_publication")

    def test_decisions_validate_atomically_and_reject_stale_source_judgment(self):
        self.candidate()
        self.grade()
        row = queue(self.store)[0]
        decision = {"id": row["id"], "manifest_sha256": row["manifest_sha256"], "decision": "approve"}
        with self.assertRaises(CrawlError):
            decisions(self.decision_file([decision, {**decision, "id": "unknown"}]), self.store)
        self.assertIsNone(queue(self.store)[0]["decision"])
        rating = row["rating"] | {"signature": "stale"}
        self.store.db.execute("UPDATE candidates SET rating=?", (json.dumps(rating),))
        self.store.db.commit()
        row = queue(self.store)[0]
        with self.assertRaises(CrawlError):
            decisions(self.decision_file([{**decision, "manifest_sha256": row["manifest_sha256"]}]), self.store)

    def test_review_embeds_source_safely_and_disables_changed_sources(self):
        self.candidate(text='EverQuest guild history &lt;/script&gt;&lt;script&gt;window.injected=true&lt;/script&gt;')
        self.grade()
        self.store.set("archive_sha", "a" * 40)
        review(SimpleNamespace(), self.store)
        output = (self.store.root / "review.html").read_text()
        self.assertNotIn("</script><script>window.injected", output)
        self.assertIn("\\u003c/script", output)
        (self.store.root / "guild.html").unlink()
        review(SimpleNamespace(), self.store)
        exported = json.loads((self.store.root / "approval-queue.json").read_text())
        self.assertIn("source_error", exported["candidates"][0])

    def test_source_path_cannot_escape_staging(self):
        row = self.candidate()
        captures = json.loads(row["captures"])
        captures[0]["path"] = "../outside.html"
        self.store.db.execute("UPDATE candidates SET captures=?", (json.dumps(captures),))
        self.store.db.commit()
        with self.assertRaises(CrawlError):
            sources(self.store, self.store.candidates()[0], 120000)

    def test_transport_budgets_persist_across_resumed_commands(self):
        self.store.set("wayback_transport", {"requests": 100, "bytes": 500, "seconds": 5, "connections": 1})
        args = SimpleNamespace(max_requests=200, max_bytes=1000, max_seconds=60,
                               max_page_bytes=512, delay=3, bytes_per_second=128000)
        with patch("acquisition.subprocess.Popen") as popen:
            process = popen.return_value
            process.stdout.readline.return_value = json.dumps({"ok": True, "result": {},
                "transport": {"requests": 0, "bytes": 0, "seconds": 0, "connections": 0}})
            process.wait.return_value = 0
            downloader = Downloader(self.store, args)
            configuration = json.loads(process.stdin.write.call_args.args[0])
            self.assertEqual(configuration["max_requests"], 100)
            self.assertEqual(configuration["max_total_bytes"], 500)
            self.assertEqual(configuration["max_seconds"], 55)
            process.stdout.readline.return_value = json.dumps({"ok": True, "result": {},
                "transport": {"requests": 10, "bytes": 100, "seconds": 3, "connections": 1}})
            downloader.call({"op": "fixture"})
            self.assertEqual(self.store.get("wayback_transport")["requests"], 110)
            downloader.close()
        args.max_requests = 110
        with patch("acquisition.subprocess.Popen") as popen, self.assertRaises(CrawlError):
            Downloader(self.store, args)
        popen.assert_not_called()

    def test_sampling_prefers_first_tier_and_keeps_completed_first_capture(self):
        row = self.candidate()
        self.store.db.execute("UPDATE candidates SET captures='[]'")
        self.store.db.commit()
        data = b'<p>EverQuest guild history</p><a href="http://guild.example/"><img alt></a>'
        url = row["url"].replace(".example/", ".example:80/")
        records = [{"timestamp": date, "url": url, "digest": "CDX", "length": "100"}
                   for date in ("19990101000000", "20011231235959")]
        calls = []
        def acquire(job):
            calls.append(job)
            if job["op"] == "list":
                return {"captures": records, "listing_limited": False, "available_rows": 2, "identity_variants": []}
            if job["timestamp"] == records[-1]["timestamp"]:
                raise CrawlError("second sample failed")
            Path(job["destination"]).write_bytes(data)
            return {"url": url, "requested_timestamp": job["timestamp"], "timestamp": job["timestamp"],
                    "timestamp_basis": "memento_datetime", "sha256": digest(data), "bytes": len(data)}
        args = SimpleNamespace(max_candidates=50, samples_per_candidate=2, retry_unresolved=False)
        with patch("acquisition.Downloader") as downloader:
            downloader.return_value.call.side_effect = acquire
            sample(args, self.store)
        current = self.store.candidates()[0]
        captures = json.loads(current["captures"])
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0]["timestamp"], "19990101000000")
        self.assertEqual(captures[0]["url"], url)
        self.assertEqual(current["state"], "sampled")
        self.assertEqual(self.store.get("sampling_result")["staged"], 1)
        self.assertEqual(len([job for job in calls if job["op"] == "list"]), 1)

    def test_cdx_identity_variants_are_unresolved_and_never_relabelled_absent(self):
        self.candidate()
        self.store.db.execute("UPDATE candidates SET captures='[]'")
        self.store.db.commit()
        args = SimpleNamespace(max_candidates=50, samples_per_candidate=2, retry_unresolved=False)
        with patch("acquisition.Downloader") as downloader:
            downloader.return_value.call.return_value = {"captures": [], "listing_limited": False, "available_rows": 1,
                                                       "identity_variants": ["https://guild.example/"]}
            sample(args, self.store)
        self.assertEqual(self.store.candidates()[0]["state"], "identity_unresolved")
        self.assertIn("unresolved", self.store.candidates()[0]["error"])


if __name__ == "__main__":
    unittest.main()
