import asyncio
import base64
import json
import math
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from eqarchives_mcp.archive import Archive, Settings, citation_url, result_from_hit


VECTOR = [1 / math.sqrt(768)] * 768
HIT = {"_id": "mailing-lists/eq_wizards/3332.json", "_source": {
    "id": "mailing-lists/eq_wizards/3332.json", "title": "Ancient Cyclops",
    "url": "https://dbsanfte.github.io/eq-archives/mailing-lists/eq_wizards/html/3332.html",
    "text_full": "An original account of the Ancient Cyclops.\nLine two.",
    "capture_date": "1999-11-05T19:21:11+00:00", "llm_guessed_date": "1999-11-05",
}}


def make_archive(es_handler=None, embedding_handler=None):
    def es_default(request):
        return httpx.Response(200, json={"hits": {"hits": [HIT]}})

    def embedding_default(request):
        return httpx.Response(200, json={"data": [{"embedding": VECTOR}]})

    return Archive(Settings(), httpx.AsyncClient(
        base_url="http://es/", transport=httpx.MockTransport(es_handler or es_default)
    ), httpx.AsyncClient(
        base_url="http://nomic/", transport=httpx.MockTransport(embedding_handler or embedding_default)
    ))


def run(coroutine):
    return asyncio.run(coroutine)


def test_hybrid_search_preserves_query_limits_fields_and_citations():
    seen = {}

    def es(request):
        assert request.method == "POST" and request.url.path == "/eq-archive/_search"
        seen["query"] = json.loads(request.content)
        return httpx.Response(200, json={"hits": {"hits": [HIT]}})

    def embed(request):
        seen["embedding"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"embedding": VECTOR}]})

    result = run(make_archive(es, embed).search("  ancient cyclops  "))
    assert result.results[0].model_dump() == {
        "id": HIT["_id"], "title": "Ancient Cyclops", "url": HIT["_source"]["url"],
    }
    assert seen["embedding"] == {"input": ["search_query: ancient cyclops"], "model": Settings.model}
    body = seen["query"]
    assert body["size"] == 10 and body["timeout"] == "8s"
    assert body["query"]["simple_query_string"]["query"] == "ancient cyclops"
    assert {item["field"] for item in body["knn"]} == {
        "text.vector", "llm_summary_vector", "llm_image_text_vector",
    }
    assert all(item["query_vector"] == VECTOR for item in body["knn"])
    assert "text_full" not in body["_source"]


@pytest.mark.parametrize("query", ['"ancient cyclops"', "cyclops -ring", "cyclops +ring", "cyclops|ring", "(cyclops)", "a" * 601])
def test_constraints_and_long_queries_use_lexical_search(query):
    def embed(request):
        pytest.fail("This query must not use semantic retrieval")

    def es(request):
        body = json.loads(request.content)
        assert "knn" not in body
        assert body["query"]["simple_query_string"]["query"] == query
        return httpx.Response(200, json={"hits": {"hits": []}})

    assert run(make_archive(es, embed).search(query)).results == []


@pytest.mark.parametrize("bad", [[], {}, {"data": []}, {"data": [{}]}, {"data": [{"embedding": [0] * 768}]},
                                     {"data": [{"embedding": [True] * 768}]},
                                     {"data": [{"embedding": ["secret"] * 768}]}])
def test_invalid_embeddings_fall_back_without_leaking_upstream_data(bad):
    def es(request):
        assert "knn" not in json.loads(request.content)
        return httpx.Response(200, json={"hits": {"hits": [HIT]}})

    result = run(make_archive(es, lambda r: httpx.Response(200, json=bad)).search("cyclops"))
    assert result.results[0].title == "Ancient Cyclops"


@pytest.mark.parametrize("failure", ["status", "timeout", "json", "infinite"])
def test_embedding_outage_is_cached_and_search_stays_available(failure):
    calls = []

    def embed(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private upstream details", request=request)
        if failure == "json":
            return httpx.Response(200, text="not JSON")
        if failure == "infinite":
            return httpx.Response(200, content=json.dumps({"data": [{"embedding": [float("inf")] * 768}]}))
        return httpx.Response(500, text="private upstream details")

    async def exercise():
        archive = make_archive(embedding_handler=embed)
        assert (await archive.search("cyclops")).results
        assert (await archive.search("jboots")).results
        assert len(calls) == 1
        archive.cooldown_until = 0
        assert (await archive.search("jboots")).results
        assert len(calls) == 2

    run(exercise())


def test_embedding_cache_expiry_eviction_and_busy_fallback():
    calls = []

    def embed(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"embedding": VECTOR}]})

    async def exercise():
        archive = make_archive(embedding_handler=embed)
        await archive.embedding("cyclops")
        assert await archive.embedding("cyclops") == VECTOR and len(calls) == 1
        archive.cache["cyclops"] = (0, VECTOR)
        await archive.embedding("cyclops")
        assert len(calls) == 2
        async with archive.embedding_lock:
            assert await archive.embedding("busy") is None
        for n in range(129):
            await archive.embedding(str(n))
        assert len(archive.cache) == 128 and "cyclops" not in archive.cache and "0" not in archive.cache
        await archive.close()
        assert archive.es.is_closed and archive.embeddings.is_closed

    run(exercise())


def test_fetch_is_an_exact_id_lookup_and_preserves_full_source_and_provenance():
    full_text = "A source longer than a normal snippet.\n" * 10000
    source = {**HIT["_source"], "text_full": full_text, "domain_name": "example.org"}

    def es(request):
        body = json.loads(request.content)
        assert body["query"] == {"ids": {"values": [HIT["_id"]]}}
        assert body["size"] == 1 and "llm_summary" not in body["_source"]
        return httpx.Response(200, json={"hits": {"hits": [{"_id": HIT["_id"], "_source": source}]}})

    doc = run(make_archive(es).fetch(HIT["_id"]))
    assert doc.text == full_text
    assert doc.metadata["text_kind"] == "extracted_source"
    assert doc.metadata["capture_date"] == HIT["_source"]["capture_date"]
    assert "not necessarily" in doc.metadata["capture_date_note"]
    assert doc.metadata["estimated_publication_date"] == "1999-11-05"
    assert "Model-generated" in doc.metadata["estimated_publication_date_note"]


@pytest.mark.parametrize("original,ocr,kind", [("original", "image words", "extracted_source"),
                                               ("", "image words", "model_generated_image_transcription"),
                                               ("", "", "unavailable")])
def test_fetch_labels_ocr_and_never_substitutes_a_generated_summary(original, ocr, kind):
    source = {"text_full": original, "llm_image_text_full": ocr, "llm_summary": "Invented secret summary"}
    archive = make_archive(lambda r: httpx.Response(200, json={"hits": {"hits": [{"_id": "image/1", "_source": source}]}}))
    doc = run(archive.fetch("image/1"))
    assert doc.metadata["text_kind"] == kind
    assert "Invented secret summary" not in doc.text
    assert "estimated_publication_date" not in doc.metadata
    if ocr:
        assert "Model-generated image transcription" in doc.text and ocr in doc.text
    else:
        assert "No extracted source text" in doc.text


@pytest.mark.parametrize("identifier", ["../_security/user", "https://attacker.invalid/", "id?x=1&y=2"])
def test_fetch_ids_cannot_become_paths_urls_or_queries(identifier):
    def es(request):
        assert request.url == "http://es/eq-archive/_search"
        assert json.loads(request.content)["query"] == {"ids": {"values": [identifier]}}
        return httpx.Response(200, json={"hits": {"hits": []}})

    with pytest.raises(ToolError, match="Document not found"):
        run(make_archive(es).fetch(identifier))


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "https://user:password@example.org", "https://[bad", "https://example.org/\nsecret", None, 5])
def test_citation_urls_reject_unsafe_values_and_encode_permalink(bad):
    url = citation_url({"url": bad}, "an/id &?ü")
    assert url.startswith("https://search.eqarchives.org/?")
    assert parse_qs(urlsplit(url).query)["filters[0][values][0]"] == ["an/id &?ü"]


def test_alternate_url_title_and_source_id_fallback():
    assert citation_url({"url": "bad", "alternate_url": "https://example.org/a"}, "a") == "https://example.org/a"
    result = result_from_hit({"_id": "es-id", "_source": {"id": "archive-id"}})
    assert result.id == "es-id" and result.title == "archive-id"
    assert "archive-id" in result.url


@pytest.mark.parametrize("failure", ["status", "timeout", "json", "missing", "partial", "shards", "huge"])
def test_es_failures_are_explicit_sanitized_and_never_partial_success(failure, monkeypatch):
    def es(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret password", request=request)
        if failure == "huge":
            return httpx.Response(200, content=b"x" * 101)
        values = {
            "status": httpx.Response(403, text="secret password"),
            "json": httpx.Response(200, text="secret password"),
            "missing": httpx.Response(200, json={"error": "secret password"}),
            "partial": httpx.Response(200, json={"timed_out": True, "hits": {"hits": [HIT]}}),
            "shards": httpx.Response(200, json={"_shards": {"failed": 1}, "hits": {"hits": [HIT]}}),
        }
        return values[failure]

    if failure == "huge":
        monkeypatch.setattr("eqarchives_mcp.archive.MAX_RESPONSE_BYTES", 100)
    with pytest.raises(ToolError) as exc:
        run(make_archive(es).fetch("id"))
    assert "secret" not in str(exc.value)
    assert "temporarily" in str(exc.value) or "too large" in str(exc.value)


def test_blank_queries_and_request_capacity():
    async def exercise():
        archive = make_archive()
        for method in (archive.search, archive.fetch):
            with pytest.raises(ToolError):
                await method("   ")
        archive.requests = asyncio.Semaphore(0)
        for method in (archive.search, archive.fetch):
            with pytest.raises(ToolError, match="busy"):
                await method("cyclops")

    run(exercise())


def test_configuration_and_credentials_are_runtime_only(tmp_path, monkeypatch):
    values = {"es_readonly_username": "readonly-user", "es_readonly_password": "dummy-password", "openai_api_key": "dummy-key"}
    for name, value in values.items():
        (tmp_path / name).write_text(value)
    monkeypatch.setenv("SECRET_DIR", str(tmp_path))
    monkeypatch.setenv("ELASTICSEARCH_URL", "http://es:9200")
    monkeypatch.setenv("EMBEDDING_URL", "http://nomic:8080")
    settings = Settings.from_env()
    archive = Archive.connect(settings)
    request = archive.es.build_request("POST", "eq-archive/_search")
    credentials = list(archive.es.auth.auth_flow(request))[0].headers["Authorization"]
    assert base64.b64decode(credentials.split()[1]).decode() == "readonly-user:dummy-password"
    assert archive.embeddings.headers["Authorization"] == "Bearer dummy-key"
    assert "Authorization" not in request.url.query.decode()
    run(archive.close())
