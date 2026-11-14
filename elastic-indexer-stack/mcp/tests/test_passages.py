import pytest

from eqarchives_mcp.passages import (
    evidence_query,
    evidence_rows,
    select_evidence,
    PASSAGE_FIELDS,
)


def test_passage_query_scopes_both_legs_to_exact_ids_and_current_model():
    ids = ["a", "b"]
    body = evidence_query("bard -cleric", ids, "gemma", [0.1] * 768)
    assert body["_source"] == ["title"] and body["timeout"] == "1s"
    assert body["query"]["bool"]["filter"] == [{"ids": {"values": ids}}]
    assert body["query"]["bool"]["minimum_should_match"] == 0
    assert body["knn"]["filter"] == [
        {"ids": {"values": ids}},
        {"term": {"embedding_model": "gemma"}},
    ]
    assert body["knn"]["k"] == 2 and body["knn"]["num_candidates"] == 100
    assert body["knn"]["inner_hits"]["_source"] == PASSAGE_FIELDS
    assert body["query"]["bool"]["should"][0]["nested"]["query"] == {
        "match": {"text.text_chunk": {"query": "bard -cleric", "operator": "or"}}
    }
    assert "knn" not in evidence_query("bard", ids, "gemma")
    for ids in ([], ["a"] * 61):
        with pytest.raises(ValueError):
            evidence_query("bard", ids, "gemma")


def hit(name, text, offset=0):
    return {
        "_source": {"title": "A discussion"},
        "inner_hits": {
            name
            + "_passage": {
                "hits": {
                    "hits": [
                        {
                            "_nested": {"field": "text", "offset": offset},
                            "_source": {"text_chunk": text, "context": "Author: Aria"},
                        }
                    ]
                }
            }
        },
    }


def test_nested_identity_and_attribution_are_preserved_and_empty_matches_ignored():
    evidence = evidence_rows(hit("semantic", "The answer.", 42))
    assert evidence["title"] == "A discussion"
    assert select_evidence(evidence) == [
        {"text_chunk": "The answer.", "context": "Author: Aria", "offset": 42}
    ]
    assert evidence_rows(hit("lexical", "  "))["passages"] == {}
    damaged = hit("semantic", "body")
    damaged["inner_hits"]["semantic_passage"]["hits"]["hits"][0]["_nested"][
        "field"
    ] = "other"
    with pytest.raises(ValueError):
        evidence_rows(damaged)
    damaged = hit("semantic", "body")
    damaged["inner_hits"]["semantic_passage"]["hits"]["hits"] *= 2
    with pytest.raises(ValueError):
        evidence_rows(damaged)


def test_complementary_evidence_falls_back_without_duplicating_the_same_text():
    source = {
        "passages": {
            "semantic": {"text_chunk": "same"},
            "lexical": {"text_chunk": "same"},
            "fallback": {"text_chunk": "first"},
        }
    }
    assert select_evidence(source, "paired") == [{"text_chunk": "same"}]
    source["passages"]["lexical"] = {"text_chunk": "second"}
    assert select_evidence(source, "paired") == [
        {"text_chunk": "same"},
        {"text_chunk": "second"},
    ]
    del source["passages"]["semantic"]
    assert select_evidence(source) == [{"text_chunk": "second"}]
    del source["passages"]["lexical"]
    assert select_evidence(source) == [{"text_chunk": "first"}]
    assert select_evidence({}) == []


@pytest.mark.parametrize(
    "damaged",
    [
        None,
        {"_source": []},
        {"inner_hits": []},
        {"inner_hits": {"semantic_passage": []}},
        {"inner_hits": {"semantic_passage": {"hits": []}}},
        {"inner_hits": {"semantic_passage": {"hits": {"hits": {}}}}},
        {"inner_hits": {"semantic_passage": {"hits": {"hits": [None]}}}},
    ],
)
def test_malformed_nested_evidence_is_rejected(damaged):
    with pytest.raises(ValueError):
        evidence_rows(damaged)


def test_equal_bodies_with_distinct_attribution_are_separate_evidence():
    source = {
        "passages": {
            "semantic": {"text_chunk": "Yes.", "context": "Author: One"},
            "lexical": {"text_chunk": "Yes.", "context": "Author: Two"},
        }
    }
    assert len(select_evidence(source, "paired")) == 2


def test_first_passage_source_is_read_only_for_unmatched_ids():
    from eqarchives_mcp.passages import fallback_query, FALLBACK_FIELDS

    body = evidence_query("bard", ["a"], "gemma", [0.1] * 768)
    assert len(body["query"]["bool"]["should"]) == 1
    fallback = fallback_query(["missing"])
    assert fallback["_source"] == FALLBACK_FIELDS and "knn" not in fallback
    assert fallback["query"]["bool"]["filter"] == [{"ids": {"values": ["missing"]}}]
    only = fallback["query"]["bool"]["should"]
    assert (
        len(only) == 1 and only[0]["nested"]["inner_hits"]["name"] == "fallback_passage"
    )
    assert only[0]["nested"]["query"] == {"match_all": {}}
    assert (
        len(
            evidence_query("bard", ["a"], "gemma", include_fallback=True)["query"][
                "bool"
            ]["should"]
        )
        == 2
    )
