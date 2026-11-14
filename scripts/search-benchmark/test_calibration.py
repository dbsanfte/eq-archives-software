import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from calibration import configure, blended
from calibration_judge import Ledger
from benchmark import BenchmarkError


class Calibration(unittest.TestCase):
    def test_rrf_uses_fixed_windows_prefilters_and_groups_after_fusion(self):
        from calibration import fused
        from copy import deepcopy

        calls = []

        def search(body):
            calls.append(deepcopy(body))
            ids = ["a", "b"] if "query" in body else ["b", "c"]
            return {
                "took": 1,
                "hits": {
                    "hits": [
                        {"_id": id, "_source": {"domain_name": "example.org"}}
                        for id in ids
                    ]
                },
            }

        snapshot = SimpleNamespace(search=search)
        frontend = SimpleNamespace(
            call=lambda args: [
                "page" if h["_id"] in ("a", "c") else "other" for h in args["hits"]
            ]
        )
        domain = {"term": {"domain_name": "example.org"}}
        body = {
            "query": {"match_all": {}},
            "post_filter": domain,
            "knn": [{"field": "text.vector", "filter": domain}],
        }
        result = fused(
            snapshot,
            frontend,
            body,
            {"filters": []},
            {"rank_window_size": 100, "rrf_constant": 60},
        )
        self.assertEqual([h["_id"] for h in result["raw"]], ["b", "a", "c"])
        self.assertEqual([h["_id"] for h in result["grouped"]], ["b", "a"])
        self.assertNotIn("knn", calls[0])
        self.assertNotIn("query", calls[1])
        self.assertEqual(calls[1]["knn"][0]["filter"], domain)
        self.assertTrue(
            all(row["size"] == 100 and row["post_filter"] == domain for row in calls)
        )
        with self.assertRaises(BenchmarkError):
            fused(snapshot, frontend, body, {}, {"rank_window_size": 1001})
        self.assertEqual(len(calls), 2)

    def test_mcp_native_ties_and_research_constraints_remain_distinct(self):
        from unittest.mock import patch
        from calibration import mcp_query
        import importlib.util

        path = (
            Path(__file__).resolve().parents[2]
            / "elastic-indexer-stack/mcp/eqarchives_mcp/ranking_policy.py"
        )
        spec = importlib.util.spec_from_file_location(
            "calibration_ranking_policy", path
        )
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)

        research = SimpleNamespace(
            keyword_query=lambda text: {"simple_query_string": {"query": text}},
            has_constraints=lambda text: '"' in text,
            research_sort=lambda order: [{"id": "asc"}],
        )
        with patch.dict(
            "sys.modules",
            {
                "eqarchives_mcp.research": research,
                "eqarchives_mcp.ranking_policy": policy,
            },
        ):
            plain = mcp_query({"text": "bard"}, [1], "model", [])
            self.assertNotIn("sort", plain)
            self.assertEqual(len(plain["knn"]), 4)
            self.assertNotIn("knn", mcp_query({"text": '"bard"'}, [1], "model", []))
            filters = [{"term": {"domain_name": "example.org"}}]
            scoped = mcp_query(
                {"text": "bard", "filters": filters}, [1], "model", filters
            )
            self.assertNotIn("knn", scoped)
            self.assertEqual(scoped["query"]["bool"]["filter"], filters)
            self.assertEqual(scoped["sort"], [{"id": "asc"}])

    def test_new_controls_preserve_baseline_and_native_constraints(self):
        body = {
            "query": {
                "simple_query_string": {
                    "query": "bard twisting",
                    "fields": ["text_full"],
                }
            },
            "knn": [
                {"field": "text.vector", "k": 40, "num_candidates": 300, "boost": 12}
            ],
        }
        self.assertEqual(configure(body, {}), body)
        result = configure(
            body,
            {
                "num_candidates": 1000,
                "similarity": 0.5,
                "minimum_should_match": "2<-25%",
            },
        )
        self.assertEqual(result["knn"][0]["k"], 40)
        self.assertEqual(result["knn"][0]["boost"], 12)
        self.assertEqual(result["knn"][0]["similarity"], 0.5)
        self.assertEqual(
            result["query"]["simple_query_string"]["minimum_should_match"], "2<-25%"
        )
        constrained = {
            "query": {
                "simple_query_string": {
                    "query": '"bard" -cleric',
                    "default_operator": "and",
                }
            }
        }
        self.assertEqual(
            configure(constrained, {"minimum_should_match": "2<-25%"}), constrained
        )

    def test_cross_snapshot_reuse_requires_complete_source_query_and_judge_identity(
        self,
    ):
        from calibration_judge import reusable_ratings
        from benchmark import digest

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ratings.jsonl"
            query = {"text": "bard twisting", "split": "tune"}
            source = {"source_sha256": "original"}
            row = {
                "query_id": "q",
                "doc_id": "d",
                "query_sha256": digest(query),
                "source_sha256": "original",
                "grader_signature": "judge",
                "model": "gpt-6-luna",
                "origin": "model",
                "grade": 3,
            }
            path.write_text(json.dumps(row) + "\n")
            self.assertEqual(
                reusable_ratings([path], {"q": query}, {"d": source}, "judge"),
                {("q", "d"): row},
            )
            self.assertFalse(
                reusable_ratings(
                    [path], {"q": query}, {"d": {"source_sha256": "changed"}}, "judge"
                )
            )
            self.assertFalse(
                reusable_ratings(
                    [path],
                    {"q": {**query, "split": "validation"}},
                    {"d": source},
                    "judge",
                )
            )
            self.assertFalse(
                reusable_ratings([path], {"q": query}, {"d": source}, "new judge")
            )

    def test_invalid_or_concurrent_budget_cannot_overspend(self):
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "budget.db"
            for cap in (float("nan"), float("inf"), 0, -1):
                with self.assertRaises(BenchmarkError):
                    Ledger(path, cap)
            ledger = Ledger(path, 0.1)

            def reserve(n):
                try:
                    ledger.reserve(str(n), {"max_output_tokens": 32768})
                    return True
                except BenchmarkError:
                    return False

            with ThreadPoolExecutor(max_workers=5) as executor:
                successes = sum(executor.map(reserve, range(20)))
            self.assertGreater(successes, 0)
            self.assertLess(successes, 20)
            self.assertLessEqual(ledger.summary()["uncertain_reserved_usd"], 0.1)

    def test_validation_cannot_spend_before_selection_and_frozen_tune_cannot_change(
        self,
    ):
        from calibration_judge import grade

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "manifest.json").write_text("{}")
            with self.assertRaisesRegex(BenchmarkError, "Freeze"):
                grade(SimpleNamespace(run=d, split="validation"))
            (root / "selection.json").write_text("{}")
            with self.assertRaisesRegex(BenchmarkError, "frozen"):
                grade(SimpleNamespace(run=d, split="tune"))

    def test_experiments_keep_filters_and_only_change_explicit_weights(self):
        body = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "simple_query_string": {
                                "query": '"bard" -cleric',
                                "fields": ["text_full"],
                                "default_operator": "and",
                            }
                        }
                    ],
                    "filter": [{"term": {"domain_name": "example.org"}}],
                }
            },
            "knn": [
                {
                    "field": "text.vector",
                    "k": 50,
                    "num_candidates": 250,
                    "boost": 10,
                    "filter": [{"term": {"embedding_model": "gemma"}}],
                },
                {"field": "image_vector", "boost": 10},
            ],
        }
        result = configure(
            body,
            {
                "k": 100,
                "num_candidates": 500,
                "vector_fields": {"text.vector": 0.5},
                "lexical_fields": {"title": 3, "text_full": 1},
            },
        )
        self.assertEqual(
            result["query"]["bool"]["filter"], body["query"]["bool"]["filter"]
        )
        self.assertEqual(
            result["query"]["bool"]["must"][0]["simple_query_string"]["query"],
            '"bard" -cleric',
        )
        self.assertEqual(result["knn"][0]["filter"], body["knn"][0]["filter"])
        self.assertEqual(result["knn"][0]["boost"], 5)
        self.assertEqual(len(result["knn"]), 1)
        self.assertEqual(body["knn"][0]["k"], 50)

    def test_blending_keeps_tail_unsupported_positions_ties_and_exact_ids(self):
        hits = [{"_id": str(n)} for n in range(25)]
        values = {str(n): n / 20 for n in range(20) if n != 3}
        self.assertEqual(blended(hits, values, 0), hits)
        ranked = blended(hits, values, 1)
        self.assertEqual(ranked[3], hits[3])
        self.assertEqual(ranked[20:], hits[20:])
        self.assertEqual(set(h["_id"] for h in ranked), set(h["_id"] for h in hits))
        self.assertEqual(blended(hits, {str(n): 0.5 for n in range(20)}, 1), hits)

    def test_dollar_reservation_and_usage_are_durable_and_retries_cannot_reset_cost(
        self,
    ):
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(Path(d) / "budget.db", 0.05)
            payload = {"max_output_tokens": 32768, "input": "test"}
            self.assertIsNone(ledger.reserve("a", payload))
            with self.assertRaises(BenchmarkError):
                ledger.reserve("a", payload)
            with self.assertRaises(BenchmarkError):
                ledger.reserve("b", payload)
            response = {
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "input_tokens_details": {"cached_tokens": 100},
                }
            }
            ledger.settle("a", response)
            self.assertEqual(ledger.reserve("a", payload), response)
            self.assertIsNone(ledger.reserve("b", payload))
            reopened = Ledger(Path(d) / "budget.db", 0.05)
            self.assertEqual(reopened.summary()["requests"], 2)
            self.assertGreater(reopened.summary()["uncertain_reserved_usd"], 0)
            self.assertAlmostEqual(reopened.summary()["estimated_usd"], 0.000341)
            with self.assertRaises(BenchmarkError):
                reopened.settle("b", {})

    def test_query_suite_has_fresh_disjoint_validation_and_preserves_negative_controls(
        self,
    ):
        root = Path(__file__).parent
        new = json.loads((root / "calibration-queries.json").read_text())
        old = {q["text"] for q in json.loads((root / "queries.json").read_text())}
        val = [q for q in new if q["split"] == "validation"]
        self.assertEqual(len(val), 24)
        self.assertFalse(old & {q["text"] for q in val})
        self.assertEqual(sum(q["category"] == "negative" for q in val), 2)
        self.assertEqual(len({q["id"] for q in new}), len(new))


class FrozenSelection(unittest.TestCase):
    def test_cannot_choose_after_validation_has_been_judged(self):
        from calibration_report import freeze

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "ratings-validation.jsonl").write_text("")
            with self.assertRaisesRegex(BenchmarkError, "Validation"):
                freeze(root)

    def test_cannot_overwrite_an_existing_selection(self):
        from calibration_report import freeze

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "selection.json").write_text("{}")
            with self.assertRaisesRegex(BenchmarkError, "frozen"):
                freeze(root)

    def test_common_query_denominator_does_not_reward_missing_judgments(self):
        from calibration_report import summarize

        configs = {
            "a": {
                "q1": {"ndcg10": 1, "precision10": 1},
                "q2": {"ndcg10": None, "precision10": None},
            },
            "b": {
                "q1": {"ndcg10": 0.5, "precision10": 0.6},
                "q2": {"ndcg10": 1, "precision10": 1},
            },
        }
        table, common = summarize(configs, ["q1", "q2"])
        self.assertEqual(common, ["q1"])
        self.assertEqual(table[1]["ndcg10"], 0.5)

    def test_frozen_selection_rejects_changed_evidence(self):
        from calibration_report import validate
        from benchmark import save, digest

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            save(root / "manifest.json", {"queries": []})
            (root / "ratings-tune.jsonl").write_text("")
            save(
                root / "selection.json",
                {"manifest_sha256": "wrong", "tune_ratings_sha256": digest([])},
            )
            with self.assertRaisesRegex(BenchmarkError, "changed"):
                validate(root)


if __name__ == "__main__":
    unittest.main()
