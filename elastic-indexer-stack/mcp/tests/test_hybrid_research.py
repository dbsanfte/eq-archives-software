import asyncio
import json
import time

import httpx
import pytest
from starlette.testclient import TestClient

from eqarchives_mcp.research import ResearchFilters, URLScope
from eqarchives_mcp.server import create_app
from test_archive import make_archive, run, VECTOR, HIT
from test_protocol import rpc


def research_archive(embed=None):
    calls = []

    def es(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"hits": {"total": {"value": 1, "relation": "eq"}, "hits": [HIT]}}
        )

    return make_archive(es, embed), calls


def test_hybrid_prefilters_include_every_independent_scope_and_date_window():
    async def scenario():
        archive, calls = research_archive()
        filters = ResearchFilters(
            domains=["example.org"],
            mailing_lists=["eqbards"],
            collections=["websites"],
            has_ocr=False,
            has_enrichment=True,
            capture_from="1999-01-01",
            indexed_to="2026-11-14",
            publication_to="2001-12-31",
            url_scopes=[URLScope(url="http://example.org/guild/", kind="directory")],
        )
        page = await archive.search_archive("song timing", filters=filters, hybrid=True)
        body = calls[0]
        assert "Hybrid keyword/semantic" in page.note and len(body["knn"]) == 4
        for branch in body["knn"]:
            assert branch["filter"][:-1] == body["query"]["bool"]["filter"]
            assert branch["filter"][-1] == {
                "term": {"embedding_model": archive.settings.model}
            }
            assert (branch["k"], branch["num_candidates"], branch["boost"]) == (
                50,
                250,
                30,
            )
        assert "song timing" in json.dumps(body["query"]["bool"]["must"])
        await archive.close()

    run(scenario())


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"hybrid": True, "query": '"song timing" -cleric'},
        {"hybrid": True, "query": ""},
        {"hybrid": True, "sort": "capture_date_desc"},
    ],
)
def test_default_constraints_empty_queries_and_date_sort_never_embed(args):
    def never(request):
        raise AssertionError("Unused embedding")

    archive, calls = research_archive(never)
    page = run(archive.search_archive(**{"query": "song timing", **args}))
    assert "knn" not in calls[0] and "Keyword" in page.note


def test_hybrid_grouping_and_metadata_directories_keep_their_contracts():
    async def scenario():
        archive, calls = research_archive()
        page = await archive.search_archive(
            "bard songs", hybrid=True, group_by="discussion"
        )
        assert page.group_by == "discussion" and page.results[0].id == HIT["_id"]
        assert "knn" in calls[0] and calls[0]["size"] == 1000
        assert "Hybrid keyword/semantic" in page.note
        await archive.close()

    run(scenario())


def test_fallback_stays_keyword_across_pages_then_recovers_after_plan_expiry():
    async def scenario():
        attempts = []

        def embed(request):
            attempts.append(1)
            return (
                httpx.Response(503)
                if len(attempts) == 1
                else httpx.Response(200, json={"data": [{"embedding": VECTOR}]})
            )

        archive, calls = research_archive(embed)
        first = await archive.search_archive("bard songs", hybrid=True)
        archive.cooldown_until = 0
        second = await archive.search_archive("bard songs", hybrid=True, offset=1)
        assert "fallback" in first.note and second.note == first.note
        assert len(attempts) == 1 and all("knn" not in c for c in calls)
        key = next(iter(archive.research_plans))
        archive.research_plans[key] = (0, None)
        recovered = await archive.search_archive("bard songs", hybrid=True)
        assert "Hybrid keyword/semantic" in recovered.note and "knn" in calls[-1]
        assert len(attempts) == 2
        await archive.close()

    run(scenario())


def test_concurrent_embedding_fallback_chooses_one_stable_research_plan():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def embed(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json={"data": [{"embedding": VECTOR}]})

        archive, calls = research_archive(embed)
        first = asyncio.create_task(archive.search_archive("bard", hybrid=True))
        await started.wait()
        second = await archive.search_archive("bard", hybrid=True, offset=1)
        release.set()
        assert (await first).note == second.note
        assert all("knn" not in c for c in calls)
        await archive.close()

    run(scenario())


def test_plan_cache_is_bounded_and_filters_have_independent_plans():
    async def scenario():
        archive, calls = research_archive()
        for n in range(65):
            await archive.search_archive(
                "bard",
                filters=ResearchFilters(domains=[f"{n}.example.org"]),
                hybrid=True,
            )
        assert len(archive.research_plans) == 64
        assert calls[-1]["knn"][0]["filter"][0] == {
            "terms": {"domain_name": ["64.example.org"]}
        }
        await archive.close()

    run(scenario())


def test_hybrid_is_an_additive_strict_boolean_in_the_public_schema():
    archive, calls = research_archive()
    with TestClient(create_app(archive)) as client:
        tool = next(
            t
            for t in rpc(client, "tools/list").json()["result"]["tools"]
            if t["name"] == "search_archive"
        )
        assert tool["inputSchema"]["properties"]["hybrid"]["default"] is False
        for bad in ("yes", 1, {}, []):
            result = rpc(
                client,
                "tools/call",
                {
                    "name": "search_archive",
                    "arguments": {"query": "bard", "hybrid": bad},
                },
            )
            assert result.json()["result"]["isError"]
        assert calls == []
        result = rpc(
            client,
            "tools/call",
            {"name": "search_archive", "arguments": {"query": "bard", "hybrid": True}},
        ).json()["result"]
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        assert "knn" in calls[0]


def test_hybrid_validates_combined_text_budget_before_embedding():
    from mcp.server.mcpserver.exceptions import ToolError

    def never(request):
        raise AssertionError("Invalid input must not start inference")

    archive, _ = research_archive(never)
    with pytest.raises(ToolError):
        run(archive.search_archive("a/" * 101, hybrid=True))
    assert not archive.research_plans and not archive.cooldown_until


def test_hybrid_prefilter_uses_verified_board_membership_in_every_branch():
    async def scenario():
        calls = []

        def es(request):
            body = json.loads(request.content)
            calls.append(body)
            if body.get("_source") == ["id", "text_full"]:
                hits = [
                    {
                        "_id": "websites/pub4.ezboard.com/20000101000000/beqasylum",
                        "_source": {
                            "text_full": "[General](http://pub8.ezboard.com/feqasylumgeneral)"
                        },
                    }
                ]
            else:
                hits = [HIT]
            return httpx.Response(
                200,
                json={"hits": {"hits": hits, "total": {"value": 1, "relation": "eq"}}},
            )

        archive = make_archive(es)
        await archive.search_archive(
            "bard",
            filters=ResearchFilters(
                url_scopes=[
                    URLScope(
                        url="http://pub4.ezboard.com/beqasylum", kind="ezboard_board"
                    )
                ]
            ),
            hybrid=True,
        )
        body = calls[-1]
        assert "feqasylumgeneral" in json.dumps(body["query"])
        for branch in body["knn"]:
            assert branch["filter"][:-1] == body["query"]["bool"]["filter"]
        await archive.close()

    run(scenario())


def test_live_research_plan_restores_its_exact_vector_for_passage_selection():
    async def scenario():
        attempts = []

        def embed(request):
            attempts.append(1)
            return httpx.Response(200, json={"data": [{"embedding": VECTOR}]})

        archive, _ = research_archive(embed)
        await archive.search_archive("bard", hybrid=True)
        archive.cache.clear()
        for n in range(128):
            archive.cache[(archive.settings.model, str(n))] = (
                time.monotonic() + 60,
                VECTOR,
            )
        await archive.search_archive("bard", hybrid=True, offset=1)
        assert len(attempts) == 1 and len(archive.cache) == 128
        assert archive.cache[archive.settings.model, "bard"][1] == VECTOR
        await archive.close()

    run(scenario())
