"""Metrics, blind human review export, and paired comparisons for a saved run."""

from collections import defaultdict
import html
import json
import math
from pathlib import Path
import random
import statistics

from benchmark import BenchmarkError, digest, grouped, jsonlines, load, save


def dcg(grades):
    return sum((2 ** grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(grades))


def scores(ranking, pool, ratings, collapse=False):
    """Recall denominator is the judged pool, never the unenumerated corpus."""
    ranked = grouped(ranking) if collapse else ranking
    def key(hit):
        return hit["capture_key"] if collapse else hit["_id"]
    known = {}
    for hit in pool:
        if hit["_id"] in ratings:
            known[key(hit)] = max(known.get(key(hit), 0), ratings[hit["_id"]])
    relevant = {identity for identity, grade in known.items() if grade >= 2}
    top = ranked[:10]
    unjudged = sum(hit["_id"] not in ratings for hit in ranked[:50])
    complete10 = all(hit["_id"] in ratings for hit in top)
    complete50 = unjudged == 0
    top_grades = [ratings.get(hit["_id"], 0) for hit in top]
    ideal = dcg(sorted(known.values(), reverse=True)[:10])
    first = next((rank for rank, grade in enumerate(top_grades, 1) if grade >= 2), None)
    found = {key(hit) for hit in ranked[:50] if ratings.get(hit["_id"], 0) >= 2}
    return {"ndcg10": dcg(top_grades) / ideal if complete10 and ideal else None,
            "precision10": sum(grade >= 2 for grade in top_grades) / 10 if complete10 else None,
            "mrr10": (1 / first if first else 0) if complete10 and relevant else None,
            "pooled_recall50": len(found & relevant) / len(relevant) if complete50 and relevant else None,
            "judged_fraction50": 1 - unjudged / min(50, len(ranked)) if ranked else 1,
            "unjudged50": unjudged, "known_relevant": len(relevant), "returned": len(ranked)}


def percentile(values, fraction):
    values = sorted(values)
    return values[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)] if values else None


def paired_interval(differences, seed=20261005):
    if not differences:
        return None
    generator = random.Random(seed)
    means = sorted(statistics.mean(generator.choices(differences, k=len(differences))) for _ in range(1000))
    return {"mean": statistics.mean(differences), "ci95": [percentile(means, 0.025), percentile(means, 0.975)], "queries": len(differences)}


def valid_ratings(directory, paths, allow_model):
    manifest, sources = load(directory / "manifest.json"), load(directory / "sources.json")
    queries = {q["id"]: q for q in manifest["queries"]}
    ratings, origins = defaultdict(dict), defaultdict(dict)
    for path in paths:
        for row in jsonlines(path):
            qid, doc = row["query_id"], row["doc_id"]
            if qid not in queries or doc not in sources:
                raise BenchmarkError("Rating references an unknown query or source")
            if row.get("query_sha256") != digest(queries[qid]) or row.get("source_sha256") != sources[doc]["source_sha256"]:
                raise BenchmarkError("Rating is stale: query or complete source hash differs")
            if type(row.get("grade")) is not int or not 0 <= row["grade"] <= 3 or row.get("origin") not in ("human", "model"):
                raise BenchmarkError("Ratings need a 0-3 integer grade and human/model origin")
            if row["origin"] == "model" and not allow_model:
                continue
            if row["origin"] == "model" and origins[qid].get(doc) == "human":
                continue
            ratings[qid][doc] = row["grade"]
            origins[qid][doc] = row["origin"]
    return ratings, origins


def report(args):
    directory = Path(args.run)
    manifest = load(directory / "manifest.json")
    if manifest["status"] != "complete":
        raise BenchmarkError("Refusing to evaluate an incomplete retrieval run")
    queries = {q["id"]: q for q in manifest["queries"]}
    ratings, origins = valid_ratings(directory, args.ratings or [], args.allow_model_ratings)
    pool = load(directory / "pool.json")
    runs = jsonlines(directory / "runs.jsonl")
    records, summary = [], {}
    for run in runs:
        qid = run["query_id"]
        row = {"query_id": qid, "config_id": run["config_id"], "split": queries[qid]["split"], "category": queries[qid]["category"],
               "raw": scores(run["raw"], pool.get(qid, []), ratings[qid]),
               "grouped": scores(run["grouped"], pool.get(qid, []), ratings[qid], collapse=True),
               "es_ms": run["es_ms"], "request_ms": run["request_ms"], "window_reached": run["window_reached"]}
        if manifest.get("depth", 50) < 50:
            for mode in ("raw", "grouped"):
                row[mode]["pooled_recall50"] = None
        records.append(row)
    for config in manifest["configs"]:
        selected = [row for row in records if row["config_id"] == config["id"]]
        summary[config["id"]] = {}
        for split in ("tune", "validation", "all"):
            rows = [row for row in selected if split == "all" or row["split"] == split]
            value = {"queries": len(rows), "p50_ms": percentile([r["request_ms"] for r in rows], 0.5), "p95_ms": percentile([r["request_ms"] for r in rows], 0.95), "by_category": {}}
            for mode in ("raw", "grouped"):
                value[mode] = {metric: statistics.mean([row[mode][metric] for row in rows if row[mode][metric] is not None])
                               if any(row[mode][metric] is not None for row in rows) else None
                               for metric in ("ndcg10", "precision10", "mrr10", "pooled_recall50", "judged_fraction50")}
                value[mode]["scorable_queries"] = sum(row[mode]["ndcg10"] is not None for row in rows)
            for category in sorted({row["category"] for row in rows}):
                values = [row["grouped"]["ndcg10"] for row in rows if row["category"] == category and row["grouped"]["ndcg10"] is not None]
                value["by_category"][category] = {"queries": len(values), "ndcg10": statistics.mean(values) if values else None}
            summary[config["id"]][split] = value
    by_config = {config["id"]: {row["query_id"]: row for row in records if row["config_id"] == config["id"]} for config in manifest["configs"]}
    baseline = by_config.get(args.baseline)
    if baseline is None:
        raise BenchmarkError("Baseline configuration is absent from the saved run")
    comparison = {}
    for config_id, rows in by_config.items():
        differences, regressions = [], []
        for qid, row in rows.items():
            before, after = baseline[qid]["grouped"]["ndcg10"], row["grouped"]["ndcg10"]
            if row["split"] == "validation" and before is not None and after is not None:
                differences.append(after - before)
            if before is not None and after is not None and after - before < -0.1:
                regressions.append({"query_id": qid, "split": row["split"], "delta_ndcg10": after - before})
        comparison[config_id] = {"validation_paired_ndcg10": paired_interval(differences), "regressions": sorted(regressions, key=lambda row: row["delta_ndcg10"])}
    all_tune = {qid for qid, q in queries.items() if q["split"] == "tune"}
    comparable = [qid for qid in all_tune if all(rows[qid]["grouped"]["ndcg10"] is not None for rows in by_config.values())]
    winner = max(by_config, key=lambda config_id: statistics.mean(by_config[config_id][qid]["grouped"]["ndcg10"] for qid in comparable)) if comparable else None
    origin_counts = {kind: sum(origin == kind for docs in origins.values() for origin in docs.values()) for kind in ("human", "model")}
    result = {"manifest_sha256": digest(manifest), "rating_origins": origin_counts, "summary": summary, "comparisons": comparison, "per_query": records,
              "tune_winner": winner, "common_scorable_tune_queries": len(comparable),
              "recommendation_status": "provisional: model judgments need human calibration and completed-corpus validation" if origin_counts["model"] else ("human judgments; verify corpus coverage and regressions before changing defaults" if origin_counts["human"] else "unjudged: collect source-based judgments before selecting defaults"),
              "limitations": ["Recall is relative to the pooled judgments, not exhaustive corpus recall.",
                              "Top-10 metrics require complete top-10 judgments; recall@50 requires complete top-50 judgments. Partial deeper pools remain explicit.",
                              "No-relevant-in-pool queries have undefined NDCG and are reported through precision and coverage.",
                              "Latency measures sequential search requests under current cluster load; it excludes browser painting, facets and cold embedding time.",
                              "Native lexical constraints remain active even for the semantic-only experiment."]}
    save(directory / "report.json", result)
    lines = ["# Search relevance pilot", "", f"Cohort: {manifest['cohort_count']['value']:,} documents; {len(queries)} queries; {len(manifest['configs'])} configurations.",
             f"Judgments: {origin_counts['human']} human, {origin_counts['model']} model.", "", "## Grouped results", "",
             "| Configuration | Tune NDCG@10 | Validation NDCG@10 | P@10 | P95 search ms |",
             "| --- | ---: | ---: | ---: | ---: |"]
    def fmt(value):
        return f"{value:.3f}" if value is not None else "unjudged"
    for config_id, values in summary.items():
        lines.append(f"| {config_id} | {fmt(values['tune']['grouped']['ndcg10'])} | {fmt(values['validation']['grouped']['ndcg10'])} | {fmt(values['all']['grouped']['precision10'])} | {fmt(values['all']['p95_ms'])} |")
    lines += ["", f"Tune winner on {len(comparable)} common scorable queries: {winner or 'none; judgments needed'}.",
              result["recommendation_status"], "", "## Limits", ""] + ["- " + text for text in result["limitations"]]
    lines += ["", "## Validation differences from " + args.baseline, ""]
    for config_id, value in comparison.items():
        interval = value["validation_paired_ndcg10"]
        if interval:
            lines.append(f"- {config_id}: {interval['mean']:+.3f}; paired bootstrap 95% interval [{interval['ci95'][0]:+.3f}, {interval['ci95'][1]:+.3f}], {interval['queries']} queries.")
    lines.append("")
    (directory / "report.md").write_text("\n".join(lines))
    print(f"Report written; {origin_counts['human']} human and {origin_counts['model']} model ratings. Tune winner: {winner or 'unjudged'}.")


def review(args):
    directory = Path(args.run)
    manifest, sources, pool = load(directory / "manifest.json"), load(directory / "sources.json"), load(directory / "pool.json")
    rows = []
    for query in manifest["queries"]:
        docs = sorted(pool.get(query["id"], []), key=lambda hit: digest([query["id"], hit["_id"], "blind-review"]))
        for hit in docs:
            source = sources[hit["_id"]]
            rows.append({"query_id": query["id"], "query": query["text"], "intent": query["intent"], "filters": query.get("filters", []),
                         "doc_id": hit["_id"], "source": source["source"], "source_sha256": source["source_sha256"], "query_sha256": digest(query)})
    payload = json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    document = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Archive relevance review</title><style>body{max-width:1000px;margin:2rem auto;padding:0 1rem;font:16px system-ui;background:#faf6ed;color:#30271c}button,input,select{font:inherit;padding:.5rem;margin:.3rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #bdb09b;padding:1rem}#intent{font-weight:600}#source{max-height:65vh;overflow:auto}a{color:#67472b}</style>
<h1>Archive relevance review</h1><p>Grade the complete original source for the stated information need. Rankings and configuration names are hidden. 0 irrelevant · 1 tangential · 2 useful · 3 directly relevant.</p>
<label>Reviewer <input id="reviewer" value="anonymous"></label><button id="download">Download human ratings</button><br>
<label>Query <select id="query"></select></label><button id="previous">Previous</button><button id="next">Next</button><span id="progress"></span>
<h2 id="title"></h2><p id="intent"></p><pre id="filters"></pre><p id="metadata"></p><a id="reader" target="_blank" rel="noopener">Open archive reader</a><p id="rating"></p>
<div id="grades"></div><pre id="source"></pre><script type="application/json" id="data">PAYLOAD</script>
<script>
const rows=JSON.parse(document.getElementById('data').textContent), key='archive-review-'+RUNHASH;
let index=0, ratings=JSON.parse(localStorage.getItem(key)||'{}');
const el=id=>document.getElementById(id), pair=row=>JSON.stringify([row.query_id,row.doc_id]);
const ids=[...new Set(rows.map(row=>row.query_id))];
for(const id of ids){const option=document.createElement('option');option.value=id;option.textContent=id+' '+rows.find(row=>row.query_id===id).query;el('query').append(option)}
function show(){const row=rows[index];if(!row){el('progress').textContent='No pooled sources';return}el('query').value=row.query_id;el('title').textContent=row.query;el('intent').textContent=row.intent;el('filters').textContent='Filters: '+JSON.stringify(row.filters);el('metadata').textContent=row.doc_id;el('source').textContent=row.source.text_full||'[No source text available: leave unjudged]';el('reader').href='https://search.eqarchives.org/document?id='+encodeURIComponent(row.doc_id);el('rating').textContent='Grade: '+(ratings[pair(row)]?.grade??'unjudged');el('progress').textContent=(index+1)+' / '+rows.length+'; '+Object.keys(ratings).length+' graded';el('source').scrollTop=0}
for(let grade=0;grade<=3;grade++){const button=document.createElement('button');button.textContent=['0 Irrelevant','1 Tangential','2 Useful','3 Direct'][grade];button.onclick=()=>{const row=rows[index];ratings[pair(row)]={query_id:row.query_id,doc_id:row.doc_id,source_sha256:row.source_sha256,query_sha256:row.query_sha256,grade,origin:'human',reviewer:el('reviewer').value,reviewed_at:new Date().toISOString()};localStorage.setItem(key,JSON.stringify(ratings));show()};el('grades').append(button)}
el('next').onclick=()=>{index=Math.min(rows.length-1,index+1);show()};el('previous').onclick=()=>{index=Math.max(0,index-1);show()};el('query').onchange=()=>{index=rows.findIndex(row=>row.query_id===el('query').value);show()};
el('download').onclick=()=>{const blob=new Blob([Object.values(ratings).map(row=>JSON.stringify(row)).join('\\n')+'\\n'],{type:'application/x-ndjson'});const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='human-ratings.jsonl';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000)};show();
</script></html>'''
    document = document.replace("RUNHASH", json.dumps(digest(manifest))).replace("PAYLOAD", payload)
    (directory / "review.html").write_text(document)
    print(f"Blind review page written for {len(rows)} query/source pairs; opens locally without a server.")
