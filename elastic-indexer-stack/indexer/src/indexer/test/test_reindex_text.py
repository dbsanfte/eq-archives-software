from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import reindex_text as job
from indexer.chunking import CHUNKING_VERSION
from indexer.html_extraction import TEXT_EXTRACTION_VERSION


def setup_record(tmp_path, text="complete source", present=True):
    record_id = "websites/example.org/20000101000000/archive.php?page=39"
    path = tmp_path / record_id
    if present:
        path.parent.mkdir(parents=True)
        path.write_text("original HTML")
    hit = {"_id": record_id, "_seq_no": 17, "_primary_term": 2, "sort": [record_id], "_source": {
        "id": record_id, "text_full": "opening paragraph", "llm_summary": "existing summary",
        "llm_tags": ["existing tag"], "llm_image_text_full": "existing OCR", "capture_date": "2000-01-01"
    }}
    client = MagicMock()
    client.update.return_value = {"result": "updated"}
    handler = MagicMock()
    handler._get_documents_from_file.return_value = [SimpleNamespace(page_content=text)]
    embedder = MagicMock()
    embedder.get_chunks_and_embeddings.return_value = [{"text_chunk": text, "vector": [0] * 768}]
    return hit, client, handler, embedder


def test_partial_atomic_update_preserves_enrichment_ocr_and_provenance(tmp_path):
    hit, client, handler, embedder = setup_record(tmp_path)
    original = deepcopy(hit["_source"])
    progress = job.Progress()
    job.reindex_record(client, "eq-archive", hit, tmp_path, handler, embedder, progress)
    args = client.update.call_args.kwargs
    assert args["id"] == hit["_id"]
    assert (args["if_seq_no"], args["if_primary_term"]) == (17, 2)
    updated = {**original, **args["doc"]}
    assert updated["text_full"] == "complete source"
    assert updated["text"][0]["text_chunk"] == "complete source"
    for field in ("llm_summary", "llm_tags", "llm_image_text_full", "capture_date"):
        assert updated[field] == original[field]
        assert field not in args["doc"]
    assert updated["text_extraction_version"] == TEXT_EXTRACTION_VERSION
    assert updated["text_chunking_version"] == CHUNKING_VERSION
    assert progress.updated == 1


def test_unchanged_text_is_still_rechunked_when_chunking_version_changes(tmp_path):
    hit, client, handler, embedder = setup_record(tmp_path, "opening paragraph")
    job.reindex_record(client, "eq-archive", hit, tmp_path, handler, embedder, job.Progress())
    embedder.get_chunks_and_embeddings.assert_called_once()
    assert "text" in client.update.call_args.kwargs["doc"]


def test_missing_source_rechunks_preserved_text_without_claiming_extraction_repair(tmp_path):
    hit, client, handler, embedder = setup_record(tmp_path, present=False)
    progress = job.Progress()
    job.reindex_record(client, "eq-archive", hit, tmp_path, handler, embedder, progress)
    assert embedder.get_chunks_and_embeddings.call_args.args[0].page_content == "opening paragraph"
    patch = client.update.call_args.kwargs["doc"]
    assert patch["text_full"] == "opening paragraph"
    assert "text_extraction_version" not in patch
    assert progress.unavailable_sources == 1
    handler._get_documents_from_file.assert_not_called()


def test_finished_versions_skip_embeddings_and_updates(tmp_path):
    hit, client, handler, embedder = setup_record(tmp_path, "opening paragraph")
    hit["_source"].update(text_chunking_version=CHUNKING_VERSION, text_extraction_version=TEXT_EXTRACTION_VERSION)
    progress = job.Progress()
    job.reindex_record(client, "eq-archive", hit, tmp_path, handler, embedder, progress)
    assert progress.unchanged == 1
    client.update.assert_not_called()
    embedder.get_chunks_and_embeddings.assert_not_called()


def test_missing_full_text_never_erases_legacy_chunks(tmp_path):
    hit, client, handler, embedder = setup_record(tmp_path, present=False)
    hit["_source"].pop("text_full")
    progress = job.Progress()
    job.reindex_record(client, "eq-archive", hit, tmp_path, handler, embedder, progress)
    assert progress.empty_text == 1
    client.update.assert_not_called()
    embedder.get_chunks_and_embeddings.assert_not_called()


@pytest.mark.parametrize("record_id", ["/etc/passwd", "websites/../../outside", "other/file"])
def test_source_ids_cannot_escape_archive(tmp_path, record_id):
    with pytest.raises(ValueError, match="supported"):
        job.source_path(tmp_path, record_id)


def test_source_symlinks_cannot_escape_archive(tmp_path):
    (tmp_path / "websites").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        job.source_path(tmp_path, "websites/file")


@pytest.mark.parametrize("fault", ["empty", "multiple", "no_chunks", "wrong_id", "bad_update"])
def test_bad_sources_or_embeddings_do_not_replace_existing_records(tmp_path, fault):
    hit, client, handler, embedder = setup_record(tmp_path)
    if fault == "empty":
        handler._get_documents_from_file.return_value[0].page_content = " "
    elif fault == "multiple":
        handler._get_documents_from_file.return_value = []
    elif fault == "no_chunks":
        embedder.get_chunks_and_embeddings.return_value = []
    elif fault == "wrong_id":
        hit["_source"]["id"] = "different ID"
    else:
        client.update.return_value = {"result": "deleted"}
    with pytest.raises(ValueError):
        job.reindex_record(client, "eq-archive", hit, tmp_path, handler, embedder, job.Progress())
    if fault != "bad_update":
        client.update.assert_not_called()


def test_live_pagination_versions_and_limit(tmp_path):
    hit, client, handler, embedder = setup_record(tmp_path)
    client.count.return_value = {"count": 20}
    client.search.return_value = {"hits": {"hits": [hit]}}
    progress = job.reindex(client, "eq-archive", tmp_path, handler, embedder, [hit["_id"]], limit=1, batch_size=1)
    assert progress.processed == progress.updated == 1
    request = client.search.call_args.kwargs
    assert request["sort"] == [{"id": "asc"}]
    assert request["seq_no_primary_term"] is True
    assert {"ids": {"values": [hit["_id"]]}} in request["query"]["bool"]["filter"]
    assert "must_not" in request["query"]["bool"]


def test_failures_continue_and_do_not_log_upstream_payloads(tmp_path, caplog):
    hit, client, handler, embedder = setup_record(tmp_path)
    client.count.return_value = {"count": 1}
    client.search.side_effect = [{"hits": {"hits": [hit]}}, {"hits": {"hits": []}}]
    embedder.get_chunks_and_embeddings.side_effect = RuntimeError("secret-bearing upstream payload")
    progress = job.reindex(client, "eq-archive", tmp_path, handler, embedder)
    assert progress.failed == progress.processed == 1
    assert client.search.call_args.kwargs["search_after"] == hit["sort"]
    assert "secret-bearing" not in caplog.text
    client.update.assert_not_called()


def test_concurrent_writer_is_left_untouched(tmp_path, monkeypatch):
    hit, client, handler, embedder = setup_record(tmp_path)
    class Conflict(Exception):
        pass
    monkeypatch.setattr(job, "ConflictError", Conflict)
    client.update.side_effect = Conflict()
    client.count.return_value = {"count": 1}
    client.search.side_effect = [{"hits": {"hits": [hit]}}, {"hits": {"hits": []}}]
    assert job.reindex(client, "eq-archive", tmp_path, handler, embedder).failed == 1


def test_repeated_upstream_failures_stop_for_checkpointed_retry(tmp_path):
    hit, client, handler, embedder = setup_record(tmp_path)
    client.count.return_value = {"count": 5}
    client.search.return_value = {"hits": {"hits": [hit] * 5}}
    embedder.get_chunks_and_embeddings.side_effect = job.OpenAIError("unavailable")
    with pytest.raises(RuntimeError, match="retry resumes"):
        job.reindex(client, "eq-archive", tmp_path, handler, embedder)
    client.update.assert_not_called()


@pytest.mark.parametrize("response", [{"timed_out": True}, {"_shards": {"failed": 1}}])
def test_partial_searches_are_not_reported_complete(tmp_path, response):
    client = MagicMock()
    client.count.return_value = {"count": 1}
    client.search.return_value = response
    with pytest.raises(ValueError, match="incomplete"):
        job.reindex(client, "eq-archive", tmp_path, MagicMock(), MagicMock())


def test_cli_uses_existing_index_and_only_additive_version_mappings(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["reindex_text", "--repo", str(tmp_path), "--limit", "1"])
    factory = MagicMock()
    factory.return_value._index_name = "eq-archive"
    monkeypatch.setattr(job, "ElasticsearchManager", factory)
    monkeypatch.setattr(job, "OpenAIManager", MagicMock())
    runner = MagicMock(return_value=job.Progress())
    monkeypatch.setattr(job, "reindex", runner)
    assert job.main() == 0
    factory.assert_called_once_with(manage_index=False)
    mappings = factory.return_value.get_client.return_value.indices.put_mapping.call_args.kwargs["properties"]
    assert mappings == {"text_extraction_version": {"type": "keyword"}, "text_chunking_version": {"type": "keyword"}}


@pytest.mark.parametrize("args", [["--limit", "-1"], ["--batch-size", "0"]])
def test_cli_rejects_invalid_limits(monkeypatch, args):
    monkeypatch.setattr("sys.argv", ["reindex_text", *args])
    with pytest.raises(SystemExit):
        job.main()
