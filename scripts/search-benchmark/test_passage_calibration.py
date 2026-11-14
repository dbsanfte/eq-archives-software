import sys
from pathlib import Path
import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "elastic-indexer-stack/mcp")
)
import passage_calibration as p


class PassageCalibrationTests(unittest.TestCase):
    def test_window_permutation_cannot_move_tail_or_unsupported_slots(self):
        hits = [{"_id": str(n)} for n in range(70)]
        values = {str(n): n / 70 for n in range(70) if n != 10}
        for window in (20, 40, 60):
            ranked = p.permute(hits, values, window)
            self.assertEqual(ranked[0], hits[window - 1])
            self.assertEqual(ranked[10], hits[10])
            self.assertEqual(ranked[window:], hits[window:])
            self.assertEqual({r["_id"] for r in ranked}, {r["_id"] for r in hits})

    def test_filtered_hybrid_uses_all_scope_filters_and_keeps_constraints_lexical(self):
        q = {
            "text": "bard songs",
            "filters": [
                {"field": "domain_name", "type": "any", "values": ["example.org"]}
            ],
        }
        filters = [
            {"terms": {"domain_name": ["example.org"]}},
            {"range": {"capture_date": {"gte": "1999-01-01"}}},
        ]
        research = SimpleNamespace(
            keyword_query=lambda text: {"simple_query_string": {"query": text}},
            has_constraints=lambda text: '"' in text,
            research_sort=lambda sort: [{"id": "asc"}],
        )
        with patch.dict(sys.modules, {"eqarchives_mcp.research": research}):
            self.check_filters(q, filters)

    def check_filters(self, q, filters):
        body = p.research_query(
            q, [0.1] * 768, "embeddinggemma-2-768-v1", filters, True
        )
        for branch in body["knn"]:
            self.assertEqual(
                branch["filter"],
                filters + [{"term": {"embedding_model": "embeddinggemma-2-768-v1"}}],
            )
        self.assertNotIn(
            "knn",
            p.research_query(q, [0.1] * 768, "embeddinggemma-2-768-v1", filters, False),
        )
        self.assertNotIn(
            "knn",
            p.research_query(
                {**q, "text": '"bard songs"'},
                [0.1] * 768,
                "embeddinggemma-2-768-v1",
                filters,
                True,
            ),
        )

    def test_fresh_suite_has_stratified_heldout_queries_and_exact_filter_scopes(self):
        root = Path(__file__).parent
        rows = json.loads((root / "passage-queries.json").read_text())
        old = {
            q["text"].casefold()
            for name in ("queries.json", "calibration-queries.json")
            if (root / name).exists()
            for q in json.loads((root / name).read_text())
        }
        self.assertEqual(len(rows), 200)
        self.assertEqual(len({q["text"].casefold() for q in rows}), 200)
        self.assertFalse(old & {q["text"].casefold() for q in rows})
        for category in {q["category"] for q in rows}:
            self.assertEqual(
                [q["split"] for q in rows if q["category"] == category].count(
                    "validation"
                ),
                5,
            )
        self.assertEqual(sum(bool(q.get("filters")) for q in rows), 40)


class RuntimeEligibility(unittest.TestCase):
    def test_only_complete_tune_latency_can_admit_a_larger_window(self):
        import tempfile
        import calibration_report as report

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(report.eligible_configs(root))
            queries = [{"id": f"q{n}", "split": "tune"} for n in range(20)]
            (root / "manifest.json").write_text(json.dumps({"queries": queries}))
            rows = [
                {
                    "query_id": q["id"],
                    "mode": mode,
                    "window": window,
                    "status": "applied",
                    "concurrent": 2,
                    "seconds": 1.5,
                }
                for q in queries
                for mode in ("legacy", "semantic", "paired")
                for window in (20, 40, 60)
            ]
            for row in rows:
                if row["mode"] == "paired" and row["window"] == 60:
                    row["seconds"] = 2.9
                if row["mode"] == "legacy" and row["window"] == 60:
                    row["status"] = "fallback"

            def save():
                (root / "latency.jsonl").write_text(
                    "".join(json.dumps(r) + "\n" for r in rows)
                )

            save()
            allowed = report.eligible_configs(root)
            self.assertIn("current-r1", allowed)
            self.assertIn("semantic-60", allowed)
            self.assertNotIn("paired-60", allowed)
            self.assertNotIn("legacy-60", allowed)
            rows[0]["query_id"] = "unseen-validation"
            save()
            with self.assertRaises(report.b.BenchmarkError):
                report.eligible_configs(root)
            rows.pop(0)
            save()
            with self.assertRaises(report.b.BenchmarkError):
                report.eligible_configs(root)


class AccountedJudgmentRetry(unittest.TestCase):
    def test_format_retry_is_bounded_charged_and_reused_without_repeating_paid_work(
        self,
    ):
        import tempfile
        from copy import deepcopy
        from calibration_judge import Ledger, complete_judgment, MODEL
        from grade import SCHEMA

        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3", 1)
            payload = {
                "instructions": "Judge relevance independently.",
                "max_output_tokens": 100,
                "text": {"format": {"schema": deepcopy(SCHEMA)}},
            }

            def response(slots):
                return {
                    "status": "completed",
                    "model": MODEL,
                    "service_tier": "default",
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(
                                        {
                                            "ratings": [
                                                {"slot": n, "grade": 2} for n in slots
                                            ]
                                        }
                                    ),
                                }
                            ],
                        }
                    ],
                }

            seen = []

            def request(body, key):
                seen.append(body)
                return response([0] if len(seen) == 1 else [0, 1])

            with patch("calibration_judge.request_judgment", side_effect=request):
                ratings, attempt = complete_judgment(
                    ledger, "batch", payload, "private-key", 2
                )
                self.assertEqual(attempt, 1)
                self.assertEqual(len(ratings), 2)
                self.assertEqual(
                    complete_judgment(ledger, "batch", payload, "private-key", 2),
                    (ratings, 1),
                )
            self.assertEqual(len(seen), 2)
            self.assertEqual(ledger.summary()["requests"], 2)
            self.assertGreater(ledger.summary()["estimated_usd"], 0)
            self.assertNotIn(
                "minItems", payload["text"]["format"]["schema"]["properties"]["ratings"]
            )
            constrained = seen[1]["text"]["format"]["schema"]["properties"]["ratings"]
            self.assertEqual((constrained["minItems"], constrained["maxItems"]), (2, 2))
            self.assertEqual(constrained["items"]["properties"]["slot"]["enum"], [0, 1])
            with patch(
                "calibration_judge.request_judgment", return_value=response([])
            ) as mock:
                with self.assertRaisesRegex(Exception, "one accounted"):
                    complete_judgment(ledger, "bad", payload, "private-key", 2)
                self.assertEqual(mock.call_count, 2)
            ledger.reserve("uncertain", payload)
            with patch("calibration_judge.request_judgment") as mock:
                with self.assertRaisesRegex(Exception, "Unsettled"):
                    complete_judgment(ledger, "uncertain", payload, "private-key", 2)
                mock.assert_not_called()
