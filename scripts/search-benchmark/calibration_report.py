"""Tune-only selection, then an independently frozen held-out comparison."""

import argparse
from collections import defaultdict
import hashlib
from pathlib import Path
import statistics

import benchmark as b
from evaluate import scores, paired_interval

BASELINE = "current-r1"


def evidence_hashes(root):
    result = {}
    for name in ("ranked.jsonl", "pool.json", "sources.json"):
        h = hashlib.sha256()
        with (root / name).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        result[name] = h.hexdigest()
    return result


def measure(root, split):
    manifest = b.load(root / "manifest.json")
    queries = {q["id"]: q for q in manifest["queries"] if q["split"] == split}
    sources = b.load(root / "sources.json")
    pool = b.load(root / "pool.json")
    ratings = defaultdict(dict)
    for r in b.jsonlines(root / f"ratings-{split}.jsonl"):
        if r["query_id"] not in queries:
            raise b.BenchmarkError("Wrong judgment split")
        if (
            r["query_sha256"] != b.digest(queries[r["query_id"]])
            or r["source_sha256"] != sources[r["doc_id"]]["source_sha256"]
        ):
            raise b.BenchmarkError("Stale evidence")
        if type(r.get("grade")) is not int or not 0 <= r["grade"] <= 3:
            raise b.BenchmarkError("Invalid relevance judgment")
        ratings[r["query_id"]][r["doc_id"]] = r["grade"]
    measured = defaultdict(lambda: defaultdict(dict))
    for r in b.jsonlines(root / "ranked.jsonl"):
        qid = r["query_id"]
        if qid not in queries:
            continue
        result = scores(
            r["raw"], pool[qid], ratings[qid], collapse=r["surface"] == "portal_grouped"
        )
        measured[r["surface"]][r["config_id"]][qid] = result
    return measured, queries


def summarize(configs, queries):
    common = [
        q
        for q in queries
        if all(rows[q]["ndcg10"] is not None for rows in configs.values())
    ]
    output = []
    for id, rows in configs.items():
        complete = [q for q in queries if rows[q]["precision10"] is not None]
        output.append(
            {
                "config_id": id,
                "ndcg10": (
                    statistics.mean(rows[q]["ndcg10"] for q in common)
                    if common
                    else None
                ),
                "precision10": (
                    statistics.mean(rows[q]["precision10"] for q in complete)
                    if complete
                    else None
                ),
                "common_queries": len(common),
                "complete_queries": len(complete),
            }
        )
    return (
        sorted(
            output,
            key=lambda r: (
                -(r["ndcg10"] or 0),
                r["config_id"] != BASELINE,
                r["config_id"],
            ),
        ),
        common,
    )


def freeze(root):
    if (root / "selection.json").exists():
        raise b.BenchmarkError("Selection already frozen")
    # Refuse to select after peeking at held-out judgments.
    if (root / "ratings-validation.jsonl").exists():
        raise b.BenchmarkError("Validation already opened")
    measured, queries = measure(root, "tune")
    selected = {}
    if set(measured) != {"portal_grouped", "mcp_raw"}:
        raise b.BenchmarkError("Both search surfaces are required")
    for surface, configs in measured.items():
        table, common = summarize(configs, queries)
        if not common or BASELINE not in configs:
            raise b.BenchmarkError("No comparable judged queries or baseline")
        selected[surface] = {
            "winner": table[0]["config_id"],
            "baseline": BASELINE,
            "leaderboard": table,
            "common_query_ids": common,
        }
    result = {
        "selection_split": "tune",
        "judgment_type": "gpt-6-luna model judgments, not human validation",
        "selected": selected,
        "manifest_sha256": b.digest(b.load(root / "manifest.json")),
        "tune_ratings_sha256": b.digest(b.jsonlines(root / "ratings-tune.jsonl")),
        "evidence_sha256": evidence_hashes(root),
    }
    b.save(root / "selection.json", result)
    print(__import__("json").dumps(result, indent=2))


def validate(root):
    selection = b.load(root / "selection.json")
    if selection["manifest_sha256"] != b.digest(
        b.load(root / "manifest.json")
    ) or selection["tune_ratings_sha256"] != b.digest(
        b.jsonlines(root / "ratings-tune.jsonl")
    ):
        raise b.BenchmarkError("Frozen selection evidence changed")
    if selection["evidence_sha256"] != evidence_hashes(root):
        raise b.BenchmarkError("Frozen rankings or sources changed")
    measured, queries = measure(root, "validation")
    results = {}
    for surface, entry in selection["selected"].items():
        # Evaluate only the frozen winner and live baseline; don't choose from validation.
        configs = {id: measured[surface][id] for id in {entry["winner"], BASELINE}}
        table, common = summarize(configs, queries)
        baseline = configs[BASELINE]
        winner = configs[entry["winner"]]
        deltas = [winner[q]["ndcg10"] - baseline[q]["ndcg10"] for q in common]
        results[surface] = {
            "winner": entry["winner"],
            "metrics": table,
            "paired_ndcg10": paired_interval(deltas),
            "regressions": [
                {
                    "query_id": q,
                    "text": queries[q]["text"],
                    "delta": winner[q]["ndcg10"] - baseline[q]["ndcg10"],
                }
                for q in common
                if winner[q]["ndcg10"] < baseline[q]["ndcg10"] - 0.1
            ],
            "per_query": {
                q: {"baseline": baseline[q], "winner": winner[q]} for q in queries
            },
        }
    b.save(root / "validation.json", results)
    print(
        __import__("json").dumps(
            {
                k: {f: v for f, v in r.items() if f != "per_query"}
                for k, r in results.items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["freeze", "validate"])
    p.add_argument("--run", type=Path, required=True)
    args = p.parse_args()
    freeze(args.run) if args.command == "freeze" else validate(args.run)
