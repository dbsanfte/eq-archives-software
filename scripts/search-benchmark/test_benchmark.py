"""Observable ranking, snapshot, judgment and source-isolation regressions."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ann import exact_documents
from benchmark import BenchmarkError, Elasticsearch, HTTP, Snapshot, build_body, digest, filter_matches, grouped, load, rrf, save, validate_suite, validate_vector
from evaluate import paired_interval, report, review, scores, valid_ratings
from grade import response_ratings


def hit(identity, capture=None, text="original source"):
    return {"_id": identity, "capture_key": capture or "record:" + identity, "_source": {"text_full": text}}


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.pool = [hit("a"), hit("b"), hit("c")]
        self.grades = {"a": 3, "b": 2, "c": 0}

    def test_perfect_ranking_and_missed_known_target(self):
        good = scores(self.pool, self.pool, self.grades)
        self.assertEqual(good["ndcg10"], 1)
        self.assertEqual(good["mrr10"], 1)
        missed = scores([hit("c"), hit("b")], self.pool, self.grades)
        self.assertLess(missed["ndcg10"], good["ndcg10"])
        self.assertEqual(missed["pooled_recall50"], 0.5)
        self.assertEqual(missed["mrr10"], 0.5)

    def test_unknown_results_are_not_assumed_irrelevant(self):
        result = scores([hit("a"), hit("unknown")], self.pool, self.grades)
        self.assertIsNone(result["ndcg10"])
        self.assertIsNone(result["precision10"])
        self.assertIsNone(result["pooled_recall50"])
        self.assertEqual(result["judged_fraction50"], 0.5)

    def test_short_pages_are_not_given_perfect_precision(self):
        self.assertEqual(scores([hit("a")], self.pool, self.grades)["precision10"], 0.1)

    def test_unjudged_deeper_results_do_not_hide_a_complete_top_ten(self):
        ranking = [hit(str(n)) for n in range(12)]
        ratings = {str(n): 3 for n in range(10)}
        result = scores(ranking, ranking, ratings)
        self.assertEqual(result["ndcg10"], 1)
        self.assertEqual(result["precision10"], 1)
        self.assertIsNone(result["pooled_recall50"])
        self.assertEqual(result["unjudged50"], 2)

    def test_no_positive_judgments_have_undefined_ndcg(self):
        result = scores(self.pool, self.pool, {"a": 0, "b": 0, "c": 0})
        self.assertIsNone(result["ndcg10"])
        self.assertIsNone(result["pooled_recall50"])
        self.assertEqual(result["precision10"], 0)

    def test_grouping_does_not_credit_an_irrelevant_selected_capture(self):
        pool = [hit("old", "page:x"), hit("new", "page:x"), hit("other")]
        result = scores([pool[0], pool[1]], pool, {"old": 0, "new": 3, "other": 0}, collapse=True)
        self.assertEqual(result["precision10"], 0)
        self.assertEqual(result["pooled_recall50"], 0)
        self.assertEqual(grouped(pool), [pool[0], pool[2]])

    def test_rrf_uses_positions_instead_of_incompatible_raw_scores(self):
        first = [{**hit("a"), "_score": 900}, {**hit("b"), "_score": 500}]
        second = [{**hit("c"), "_score": 0.9}, {**hit("b"), "_score": 0.8}]
        self.assertEqual([row["_id"] for row in rrf(first, second, 60)], ["b", "a", "c"])

    def test_exact_nested_search_uses_nearest_chunk_per_parent(self):
        sample = [{"_id": "a", "_source": {"text": [{"vector": [0, 1]}, {"vector": [1, 0]}]}},
                  {"_id": "b", "_source": {"text": [{"vector": [0.5, 0.5]}]}}]
        self.assertEqual(exact_documents(sample, [1, 0])[0], ("a", 1))

    def test_paired_interval_is_reproducible_and_handles_no_data(self):
        self.assertIsNone(paired_interval([]))
        self.assertEqual(paired_interval([0.1, 0.1])["ci95"], [0.1, 0.1])
        self.assertEqual(paired_interval([0.2, -0.1, 0.05]), paired_interval([0.2, -0.1, 0.05]))


class RequestTests(unittest.TestCase):
    def exported(self):
        return {"semantic_allowed": True, "body": {"query": {"match": {"text_full": "bard"}},
                 "post_filter": {"term": {"domain_name": "example.org"}},
                 "knn": [{"field": "text.vector", "query_vector": [1], "k": 10, "num_candidates": 100,
                          "boost": 5, "filter": [{"term": {"domain_name": "example.org"}}]},
                         {"field": "llm_summary_vector", "query_vector": [1], "k": 10, "num_candidates": 100, "boost": 5}]}}

    def test_filters_apply_before_vector_selection_and_to_lexical_search(self):
        exported = self.exported()
        cohort = {"term": {"text_chunking_version": "new"}}
        result = build_body(exported, {"mode": "hybrid"}, cohort, {"boost": 5})
        for branch in result["knn"]:
            self.assertIn(cohort, branch["filter"])
        self.assertIn(cohort, result["query"]["bool"]["filter"])
        self.assertEqual(exported, self.exported(), "Construction must not mutate the cached baseline")

    def test_source_only_and_lexical_experiments(self):
        result = build_body(self.exported(), {"mode": "semantic", "vector_fields": {"text.vector": 1}, "boost": 2, "similarity": 0.6}, None, {"boost": 5})
        self.assertNotIn("query", result)
        self.assertEqual(len(result["knn"]), 1)
        self.assertEqual(result["knn"][0]["boost"], 2)
        self.assertEqual(result["knn"][0]["similarity"], 0.6)
        lexical = build_body(self.exported(), {"mode": "lexical"}, None, {"boost": 5})
        self.assertNotIn("knn", lexical)

    def test_operator_queries_keep_their_lexical_constraints(self):
        exported = self.exported()
        exported["semantic_allowed"] = False
        result = build_body(exported, {"mode": "semantic"}, None, {"boost": 5})
        self.assertIn("query", result)
        self.assertNotIn("knn", result)

    def test_missing_semantic_branches_fail_instead_of_silent_fallback(self):
        exported = self.exported()
        del exported["body"]["knn"]
        with self.assertRaises(BenchmarkError):
            build_body(exported, {"mode": "hybrid"}, None, {"boost": 5})

    def test_inclusive_dates_and_missing_date_rejection(self):
        filters = [{"field": "capture_date", "type": "range", "values": [{"from": "2000-01-01T00:00:00Z", "to": "2000-01-02T23:59:59.999Z", "isDateField": True}]}]
        self.assertTrue(filter_matches({"capture_date": "2000-01-02T23:59:59.999Z"}, filters))
        self.assertFalse(filter_matches({}, filters))
        self.assertFalse(filter_matches({"capture_date": "2000-01-03T00:00:00Z"}, filters))

    def test_any_all_and_exclusion_facets(self):
        for mode, values, expected in (("all", ["a", "b"], True), ("any", ["a", "c"], True), ("none", ["a"], False), ("all", ["c"], False)):
            self.assertEqual(filter_matches({"llm_tags": ["a", "b"]}, [{"field": "llm_tags", "type": mode, "values": values}]), expected)

    def test_invalid_dimensions_and_nonfinite_vectors(self):
        for vector in ([0.1], [float("nan")] * 768, [0] * 768):
            with self.assertRaises(BenchmarkError):
                validate_vector(vector)
        self.assertEqual(len(validate_vector([1 / 768 ** 0.5] * 768)), 768)

    def test_elasticsearch_transport_cannot_write_documents_or_mappings(self):
        client = Elasticsearch("http://localhost:9200")
        for method, path in (("POST", "/eq-archive/_bulk"), ("POST", "/eq-archive/_update/x"), ("DELETE", "/eq-archive"), ("PUT", "/eq-archive/_mapping")):
            with self.assertRaises(BenchmarkError):
                client.request(method, path, {})

    def test_credentials_in_endpoint_are_rejected(self):
        for endpoint in ("http://user:password@localhost", "http://localhost?key=secret", "file:///tmp/private"):
            with self.assertRaises(BenchmarkError):
                HTTP(endpoint)

    def test_failed_response_does_not_include_upstream_payload(self):
        import urllib.error
        error = urllib.error.HTTPError("http://host", 401, "secret password", {}, None)
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(BenchmarkError) as raised:
                HTTP("http://host").request("POST", "/search", {})
        self.assertNotIn("secret", str(raised.exception))

    def test_snapshot_rejects_partial_results_and_closes_latest_handle(self):
        class Fake:
            def __init__(self): self.calls = []
            def request(self, method, path, body=None):
                self.calls.append((method, path, body))
                if method == "POST" and path.endswith("keep_alive=2m"):
                    return {"id": "first"}
                if path == "/_search":
                    return {"pit_id": "latest", "timed_out": True}
                return {}
        fake = Fake()
        with self.assertRaises(BenchmarkError):
            with Snapshot(fake, "eq-archive") as snapshot:
                snapshot.search({"size": 1})
        self.assertEqual(fake.calls[-1], ("DELETE", "/_pit", {"id": "latest"}))


class JudgmentTests(unittest.TestCase):
    def fixture(self, directory):
        queries = [{"id": "t", "text": "bard", "intent": "bard mechanics", "split": "tune", "category": "names"},
                   {"id": "v", "text": "charm", "intent": "charm mechanics", "split": "validation", "category": "questions"}]
        sources = {"a": {"source": {"text_full": "RUNHASH </script><script>unsafe</script>"}, "source_sha256": "source-a"},
                   "b": {"source": {"text_full": "second complete source"}, "source_sha256": "source-b"}}
        manifest = {"status": "complete", "cohort_count": {"value": 2}, "queries": queries,
                    "configs": [{"id": "current-hybrid"}, {"id": "candidate"}]}
        save(directory / "manifest.json", manifest)
        save(directory / "sources.json", sources)
        save(directory / "pool.json", {"t": [hit("a"), hit("b")], "v": [hit("a"), hit("b")]})
        return queries

    def rating(self, query, doc, grade, origin="human"):
        return {"query_id": query["id"], "doc_id": doc, "grade": grade, "origin": origin,
                "source_sha256": "source-" + doc, "query_sha256": digest(query)}

    def test_model_grades_are_opt_in_and_human_review_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            queries = self.fixture(directory)
            rows = [self.rating(queries[0], "a", 2), self.rating(queries[0], "a", 0, "model"), self.rating(queries[0], "b", 3, "model")]
            path = directory / "ratings.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows))
            ratings, origins = valid_ratings(directory, [path], False)
            self.assertEqual(ratings["t"], {"a": 2})
            ratings, origins = valid_ratings(directory, [path], True)
            self.assertEqual(ratings["t"], {"a": 2, "b": 3})

    def test_stale_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            query = self.fixture(directory)[0]
            row = self.rating(query, "a", 2)
            row["source_sha256"] = "old"
            path = directory / "ratings.jsonl"
            path.write_text(json.dumps(row))
            with self.assertRaises(BenchmarkError):
                valid_ratings(directory, [path], True)

    def test_validation_does_not_choose_the_tune_winner(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            queries = self.fixture(directory)
            path = directory / "ratings.jsonl"
            path.write_text("\n".join(json.dumps(self.rating(query, doc, grade)) for query in queries for doc, grade in (("a", 3), ("b", 0))))
            rows = []
            for qid in ("t", "v"):
                for config in ("current-hybrid", "candidate"):
                    good = (qid == "t") == (config == "candidate")
                    ranking = [hit("a"), hit("b")] if good else [hit("b"), hit("a")]
                    rows.append({"query_id": qid, "config_id": config, "raw": ranking, "grouped": ranking, "es_ms": 5, "request_ms": 10, "window_reached": False})
            (directory / "runs.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
            with redirect_stdout(io.StringIO()):
                report(SimpleNamespace(run=directory, ratings=[path], allow_model_ratings=False, baseline="current-hybrid"))
            result = load(directory / "report.json")
            self.assertEqual(result["tune_winner"], "candidate")
            self.assertLess(result["comparisons"]["candidate"]["validation_paired_ndcg10"]["mean"], 0)
            manifest = load(directory / "manifest.json")
            manifest["depth"] = 10
            save(directory / "manifest.json", manifest)
            with redirect_stdout(io.StringIO()):
                report(SimpleNamespace(run=directory, ratings=[path], allow_model_ratings=False, baseline="current-hybrid"))
            self.assertIsNone(load(directory / "report.json")["summary"]["candidate"]["all"]["grouped"]["pooled_recall50"])

    def test_offline_review_escapes_source_and_preserves_complete_text(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            self.fixture(directory)
            with redirect_stdout(io.StringIO()):
                review(SimpleNamespace(run=directory))
            text = (directory / "review.html").read_text()
            self.assertNotIn("</script><script>unsafe", text)
            payload = text.split('<script type="application/json" id="data">')[1].split("</script>")[0]
            rows = json.loads(payload)
            self.assertIn("RUNHASH </script><script>unsafe</script>", [row["source"]["text_full"] for row in rows])

    def test_grader_validates_slots_and_incomplete_responses(self):
        response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"ratings": [{"slot": 0, "grade": 3}]})}]}]}
        self.assertEqual(response_ratings(response, 1)[0]["grade"], 3)
        with self.assertRaises(BenchmarkError):
            response_ratings(response, 2)
        response["status"] = "incomplete"
        with self.assertRaises(BenchmarkError):
            response_ratings(response, 1)

    def test_checked_in_suite_is_valid_and_has_a_reserved_validation_set(self):
        directory = Path(__file__).resolve().parent
        queries, configs = load(directory / "queries.json"), load(directory / "configs.json")
        validate_suite(queries, configs)
        self.assertGreaterEqual(len(queries), 50)
        self.assertGreaterEqual(sum(q["split"] == "validation" for q in queries), 15)
        broken = deepcopy(configs)
        broken[0]["k"] = 500
        broken[0]["num_candidates"] = 100
        with self.assertRaises(BenchmarkError):
            validate_suite(queries, broken)


if __name__ == "__main__":
    unittest.main()
