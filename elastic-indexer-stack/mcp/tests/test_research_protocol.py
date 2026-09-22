import json
import asyncio

import httpx2
import pytest
from jsonschema import validate
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from starlette.testclient import TestClient

from eqarchives_mcp.server import create_app
from test_archive import make_archive
from test_protocol import rpc
from test_archive import HIT
from test_research import search_response, never_embed


def test_research_tools_are_discoverable_without_changing_legacy_contracts():
    with TestClient(create_app(make_archive())) as client:
        tools = {tool['name']: tool for tool in rpc(client, 'tools/list').json()['result']['tools']}
        assert set(tools) == {'search', 'fetch', 'search_archive', 'list_sources'}
        assert set(tools['search']['inputSchema']['properties']) == {'query'}
        assert set(tools['fetch']['inputSchema']['properties']) == {'id'}
        for tool in tools.values():
            assert tool['annotations']['readOnlyHint'] is True
            assert tool['annotations']['destructiveHint'] is False
            assert tool['_meta']['securitySchemes'] == [{'type': 'noauth'}]


@pytest.mark.parametrize("name,args", [
    ("search_archive", {"query": 1}), ("search_archive", {"query": "x" * 1001}),
    ("search_archive", {"limit": 0}), ("search_archive", {"limit": 51}),
    ("search_archive", {"limit": True}), ("search_archive", {"limit": "20"}),
    ("search_archive", {"offset": -1}), ("search_archive", {"offset": 1000}),
    ("search_archive", {"offset": True}), ("search_archive", {"sort": "_script"}),
    ("search_archive", {"filters": {"date_from": "1999-02-29"}}),
    ("search_archive", {"filters": {"date_from": "1999-1-01"}}),
    ("search_archive", {"filters": {"date_to": "now/d"}}),
    ("search_archive", {"filters": {"date_from": 915148800}}),
    ("search_archive", {"filters": {"date_from": "2000-01-01", "date_to": "1999-12-31"}}),
    ("search_archive", {"filters": {"date_field": "last_indexed"}}),
    ("search_archive", {"filters": {"domains": [" "]}}),
    ("search_archive", {"filters": {"domains": ["x"] * 11}}),
    ("search_archive", {"filters": {"domains": ["x" * 256]}}),
    ("search_archive", {"filters": {"domains": [1]}}),
    ("search_archive", {"filters": {"elasticsearch_query": {"match_all": {}}}}),
    ("list_sources", {"source_type": "_security"}),
    ("list_sources", {"limit": 101}), ("list_sources", {"limit": 0}),
    ("list_sources", {"after": {"script": "x"}}), ("list_sources", {"after": ""}),
    ("list_sources", {"after": "x" * 256}),
    ("list_sources", {"filters": {"date_to": "bad"}}),
])
def test_invalid_research_arguments_are_rejected_before_any_upstream_call(name, args):
    archive = make_archive(lambda r: pytest.fail("Invalid input reached Elasticsearch"), never_embed)
    with TestClient(create_app(archive)) as client:
        result = rpc(client, "tools/call", {"name": name, "arguments": args}).json()["result"]
        assert result["isError"]


def research_fixture(request):
    body = json.loads(request.content)
    if "aggs" in body:
        return search_response(aggregations={"sources": {"buckets": [{"key": {"value": "example.org"}, "doc_count": 1}]}})
    return search_response([HIT], 1)


def test_both_research_tools_return_matching_json_and_declared_output_schemas():
    with TestClient(create_app(make_archive(research_fixture, never_embed))) as client:
        tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        for name in ("search_archive", "list_sources"):
            result = rpc(client, "tools/call", {"name": name, "arguments": {
                "query": "cyclops", "filters": {"date_from": "1999-01-01", "domains": ["example.org"]},
            }}).json()["result"]
            assert not result.get("isError")
            assert len(result["content"]) == 1
            assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
            validate(result["structuredContent"], tools[name]["outputSchema"])


def test_official_sdk_client_accepts_nested_filters_and_research_outputs():
    async def exercise():
        app = create_app(make_archive(research_fixture, never_embed))
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app)) as client:
                async with streamable_http_client("http://testserver/mcp", http_client=client) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        sources = await session.call_tool("list_sources", {"query": "cyclops"})
                        assert not sources.is_error
                        value = sources.structured_content["sources"][0]["value"]
                        results = await session.call_tool("search_archive", {"query": "cyclops", "filters": {"domains": [value]}})
                        assert not results.is_error and results.structured_content["results"][0]["id"] == HIT["_id"]
    asyncio.run(exercise())
