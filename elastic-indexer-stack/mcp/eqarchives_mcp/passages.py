"""Private, ID-scoped passage evidence. Never accepts caller-supplied ES DSL."""

import hashlib
import json

PASSAGE_FIELDS = ["text.text_chunk", "text.title", "text.context"]
FALLBACK_FIELDS = ["title", "text_full", "llm_image_text_full"]
MAX_CANDIDATES = 60


def evidence_query(query, ids, model, vector=None, include_fallback=False):
    if not ids or len(ids) > MAX_CANDIDATES:
        raise ValueError("Passage candidate bound")

    def nested(name, selection):
        return {
            "nested": {
                "path": "text",
                "score_mode": "max",
                "query": selection,
                "inner_hits": {"name": name, "size": 1, "_source": PASSAGE_FIELDS},
            }
        }

    body = {
        "size": len(ids),
        "timeout": "1s",
        "track_total_hits": False,
        "_source": ["title"],
        "query": {
            "bool": {
                "filter": [{"ids": {"values": ids}}],
                "minimum_should_match": 0,
                "should": [
                    nested(
                        "lexical_passage",
                        {
                            "match": {
                                "text.text_chunk": {"query": query, "operator": "or"}
                            }
                        },
                    ),
                ],
            }
        },
    }
    if include_fallback:
        body["query"]["bool"]["should"].append(
            nested("fallback_passage", {"match_all": {}})
        )
    if vector is not None:
        body["knn"] = {
            "field": "text.vector",
            "query_vector": vector,
            "k": len(ids),
            "num_candidates": max(100, len(ids)),
            "filter": [{"ids": {"values": ids}}, {"term": {"embedding_model": model}}],
            "inner_hits": {
                "name": "semantic_passage",
                "size": 1,
                "_source": PASSAGE_FIELDS,
            },
        }
    return body


def fallback_query(ids):
    # Only unmatched records pay for the first-passage/reader fallback. Repeated
    # nested source extraction is expensive on long captured forum threads.
    body = evidence_query("", ids, "", include_fallback=True)
    body["_source"] = FALLBACK_FIELDS
    body["query"]["bool"]["should"] = body["query"]["bool"]["should"][-1:]
    return body


def evidence_rows(hit):
    """Retain nested source identity; malformed evidence cannot become a passage."""
    if (
        not isinstance(hit, dict)
        or not isinstance(hit.get("_source", {}), dict)
        or not isinstance(hit.get("inner_hits", {}), dict)
    ):
        raise ValueError("Passage document")
    selected = {}
    for name in ("semantic", "lexical", "fallback"):
        group = hit.get("inner_hits", {}).get(name + "_passage", {})
        if not isinstance(group, dict) or not isinstance(group.get("hits", {}), dict):
            raise ValueError("Passage envelope")
        rows = group.get("hits", {}).get("hits", [])
        if not isinstance(rows, list) or len(rows) > 1:
            raise ValueError("Passage evidence count")
        if not rows:
            continue
        row = rows[0]
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("_nested", {}), dict)
            or not isinstance(row.get("_source", {}), dict)
        ):
            raise ValueError("Passage row")
        identity = row.get("_nested", {})
        if (
            identity.get("field") != "text"
            or type(identity.get("offset")) is not int
            or identity["offset"] < 0
        ):
            raise ValueError("Passage identity")
        source = row.get("_source", {})
        # Elasticsearch extracts the selected nested object from its parent source.
        if isinstance(source.get("text"), dict):
            source = source["text"]
        if (
            not isinstance(source.get("text_chunk"), str)
            or not source["text_chunk"].strip()
        ):
            continue
        selected[name] = {**source, "offset": identity["offset"]}
    return {"title": hit.get("_source", {}).get("title"), "passages": selected}


def select_evidence(source, mode="semantic"):
    rows = source.get("passages", {})
    names = ("semantic", "lexical") if mode == "paired" else (mode,)
    result, seen = [], set()
    for name in names:
        row = rows.get(name)
        if row is None:
            continue
        # Same passage returned by both retrieval legs is scored only once.
        identity = hashlib.sha256(
            json.dumps(
                [row.get(k) for k in ("text_chunk", "title", "context")],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        if identity not in seen:
            seen.add(identity)
            result.append(row)
    if not result:
        row = rows.get("lexical") or rows.get("fallback")
        if row is not None:
            result.append(row)
    return result
