import asyncio
import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from eqarchives_mcp.research import ResearchFilters
from test_archive import HIT, make_archive, run


def never_embed(request):
    pytest.fail("Structured keyword research must not request embeddings")


def search_response(hits=(), total=0, relation="eq", **extra):
    return httpx.Response(200, json={"hits": {"hits": list(hits), "total": {"value": total, "relation": relation}}, **extra})


def test_pages_beyond_ten_have_disjoint_ids_and_preserve_provenance():
    documents = [{"_id": f"source/{i:03d}", "_source": {**HIT["_source"], "id": f"source/{i:03d}"}} for i in range(25)]

    def es(request):
        body = json.loads(request.content)
        assert request.url.path == "/eq-archive/_search"
        assert body["sort"] == [{"_score": "desc"}, {"id": {"order": "asc", "missing": "_last"}}, {"_doc": "asc"}]
        assert request.url.params["preference"] == "eqarchives-mcp-research"
        assert body["track_total_hits"] == 1001 and body["timeout"] == "8s"
        assert not {"text", "text_full", "llm_summary", "llm_image_text_full"} & set(body["_source"])
        assert "knn" not in body
        start = body["from"]
        return search_response(documents[start:start + body["size"]], 25)

    async def exercise():
        archive = make_archive(es, never_embed)
        ids, offset = [], 0
        while offset is not None:
            page = await archive.search_archive("cyclops", offset=offset, limit=10)
            ids.extend(result.id for result in page.results)
            assert page.total.value == 25 and page.total.relation == "eq"
            assert page.results[0].metadata["estimated_publication_date"] == "1999-11-05"
            assert "Model-generated" in page.results[0].metadata["estimated_publication_date_note"]
            assert not page.limit_reached and "live index" in page.note
            offset = page.next_offset
        assert ids == [d["_id"] for d in documents]
        assert len(set(ids)) == 25 and not page.has_more

    run(exercise())


@pytest.mark.parametrize("date_field,index_field", [("capture_date", "capture_date"), ("estimated_publication_date", "llm_guessed_date")])
def test_source_categories_are_conjoined_and_date_range_includes_whole_utc_days(date_field, index_field):
    def es(request):
        body = json.loads(request.content)
        selection = body["query"]["bool"]
        assert selection["must"][0]["simple_query_string"]["default_operator"] == "and"
        assert selection["must"][0]["simple_query_string"]["query"] == '"ancient cyclops" -ring'
        assert selection["filter"] == [
            {"terms": {"domain_name": ["example.org", "other.org"]}},
            {"terms": {"mailing_list_name": ["EQWizards"]}},
            {"terms": {"file_type": ["html", "txt"]}},
            {"range": {index_field: {"gte": "1999-01-01T00:00:00.000Z", "lte": "2000-02-29T23:59:59.999Z"}}},
        ]
        return search_response()

    filters = ResearchFilters(domains=["example.org", "other.org"], mailing_lists=["EQWizards"],
        file_types=["html", "txt"], date_field=date_field, date_from="1999-01-01", date_to="2000-02-29")
    page = run(make_archive(es, never_embed).search_archive('"ancient cyclops" -ring', filters))
    assert page.results == [] and page.next_offset is None and not page.has_more


@pytest.mark.parametrize("bounds,expected", [
    ({"date_from": "1999-11-05"}, {"gte": "1999-11-05T00:00:00.000Z"}),
    ({"date_to": "1999-11-05"}, {"lte": "1999-11-05T23:59:59.999Z"}),
])
def test_open_ended_dates_and_filter_only_browsing(bounds, expected):
    def es(request):
        selection = json.loads(request.content)["query"]["bool"]
        assert selection["must"] == [{"match_all": {}}]
        assert selection["filter"] == [{"range": {"capture_date": expected}}]
        return search_response()
    run(make_archive(es, never_embed).search_archive(filters=ResearchFilters(**bounds)))


@pytest.mark.parametrize("sort,field,direction", [
    ("capture_date_asc", "capture_date", "asc"), ("capture_date_desc", "capture_date", "desc"),
    ("estimated_publication_date_asc", "llm_guessed_date", "asc"),
    ("estimated_publication_date_desc", "llm_guessed_date", "desc"),
])
def test_date_sort_keeps_unknown_dates_last_and_breaks_ties_by_id(sort, field, direction):
    def es(request):
        assert json.loads(request.content)["sort"] == [{field: {"order": direction, "missing": "_last"}}, {"id": {"order": "asc", "missing": "_last"}}, {"_doc": "asc"}]
        return search_response()
    run(make_archive(es, never_embed).search_archive(sort=sort))


def test_legacy_records_without_source_id_remain_retrievable_and_have_a_tiebreaker():
    def es(request):
        body = json.loads(request.content)
        assert request.url.params["preference"] == "eqarchives-mcp-research"
        assert body["sort"][-1] == {"_doc": "asc"}
        assert body["query"]["bool"]["filter"] == []
        return search_response([{"_id": "legacy/path.txt", "_source": {"title": "Legacy source"}}], 1)
    page = run(make_archive(es, never_embed).search_archive())
    assert page.results[0].id == "legacy/path.txt"
    assert "legacy%2Fpath.txt" in page.results[0].url


@pytest.mark.parametrize("total,relation,more", [(1001, "gte", True), (1500, "eq", True), (1000, "eq", False)])
def test_research_window_is_bounded_without_claiming_results_are_exhausted(total, relation, more):
    def es(request):
        body = json.loads(request.content)
        assert body["from"] == 995 and body["size"] == 5
        return search_response([HIT] * 5, total, relation)
    page = run(make_archive(es, never_embed).search_archive(offset=995, limit=50))
    assert page.limit == 5 and page.next_offset is None
    assert page.has_more is more and page.limit_reached is more
    assert ("narrow the query" in page.note) is more
    assert page.total.relation == relation


def test_source_paging_uses_es_after_key_instead_of_last_bucket():
    seen = []
    def es(request):
        body = json.loads(request.content)
        composite = body["aggs"]["sources"]["composite"]
        assert body["size"] == 0 and body["_source"] is False
        assert body["query"]["bool"]["filter"] == [{"terms": {"file_type": ["html"]}}]
        assert body["query"]["bool"]["must_not"] == [{"term": {"domain_name": ""}}]
        assert composite["sources"] == [{"value": {"terms": {"field": "domain_name", "order": "asc"}}}]
        seen.append(composite.get("after"))
        if "after" not in composite:
            return search_response(aggregations={"sources": {"buckets": [{"key": {"value": "alpha.org"}, "doc_count": 12}],
                                                             "after_key": {"value": "bravo.org"}}})
        assert composite["after"] == {"value": "bravo.org"}
        return search_response(aggregations={"sources": {"buckets": []}})
    async def exercise():
        archive = make_archive(es, never_embed)
        filters = ResearchFilters(file_types=["html"])
        page = await archive.list_sources(query="cyclops", filters=filters, limit=1)
        assert page.sources[0].model_dump() == {"value": "alpha.org", "document_count": 12}
        assert page.next_after == "bravo.org"
        last = await archive.list_sources(query="cyclops", filters=filters, after=page.next_after, limit=1)
        assert last.sources == [] and last.next_after is None
    run(exercise())
    assert seen == [None, {"value": "bravo.org"}]


@pytest.mark.parametrize("kind,field", [("mailing_list", "mailing_list_name"), ("file_type", "file_type")])
def test_other_source_directories_are_scoped_to_fixed_keyword_fields(kind, field):
    def es(request):
        body = json.loads(request.content)
        assert body["aggs"]["sources"]["composite"]["sources"][0]["value"]["terms"]["field"] == field
        return search_response(aggregations={"sources": {"buckets": [{"key": {"value": "value"}, "doc_count": 2}]}})
    page = run(make_archive(es, never_embed).list_sources(kind))
    assert page.source_type == kind and page.next_after is None


@pytest.mark.parametrize("method", ["search_archive", "list_sources"])
def test_research_shares_request_limit_and_does_not_queue_when_busy(method):
    archive = make_archive(lambda r: pytest.fail("Busy calls must not reach Elasticsearch"), never_embed)
    archive.requests = asyncio.Semaphore(0)
    with pytest.raises(ToolError, match="busy"):
        run(getattr(archive, method)())


@pytest.mark.parametrize("method,extra", [
    ("search_archive", {"hits": {"hits": [], "total": {"value": -1, "relation": "eq"}}}),
    ("search_archive", {"hits": {"hits": [], "total": "secret password"}}),
    ("list_sources", {}),
    ("list_sources", {"aggregations": {"sources": {"buckets": [{"key": {"value": "x"}, "doc_count": "secret password"}]}}}),
    ("list_sources", {"aggregations": {"sources": {"buckets": [{"key": {"value": "x"}, "doc_count": 1}], "after_key": []}}}),
])
def test_malformed_research_results_fail_without_upstream_details(method, extra):
    body = {"hits": {"hits": []}, **extra}
    archive = make_archive(lambda r: httpx.Response(200, json=body))
    with pytest.raises(ToolError, match="temporarily") as error:
        run(getattr(archive, method)(limit=1))
    assert "secret" not in str(error.value)
