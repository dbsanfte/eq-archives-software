import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import index_blueworld as recovery_job
from indexer.es_manager import ElasticsearchManager


def test_split_text_preserves_all_characters():
    source = "word " * 260 + "tail"
    chunks = recovery_job.split_text(source, max_chars=120)
    assert len(chunks) > 1
    assert all(len(chunk) <= 120 for chunk in chunks)
    assert "".join(chunks) == source
    assert recovery_job.split_text("x" * 300, max_chars=120) == ["x" * 120, "x" * 120, "x" * 60]


def test_archive_embeddings_prefix_documents_and_split_oversized_requests():
    embedder = recovery_job.BoundedArchiveEmbedder.__new__(recovery_job.BoundedArchiveEmbedder)
    embedder._request_pause = 0
    requests = []

    def embed_text(value):
        requests.append(value)
        if len(value) > 40:
            raise RuntimeError("input is too large to process")
        return [0.0] * 768

    embedder.embed_text = embed_text
    result = embedder.get_chunks_and_embeddings(SimpleNamespace(page_content="A" * 100))
    assert len(result) > 1
    assert "".join(chunk["text_chunk"] for chunk in result) == "A" * 100
    assert all(request.startswith("search_document: ") for request in requests)
    assert all(len(chunk["vector"]) == 768 for chunk in result)


def test_verified_paths_rejects_changed_or_missing_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery_job, "EXPECTED_COUNT", 2)
    directory = tmp_path / recovery_job.RELATIVE_DIR
    directory.mkdir(parents=True)
    header = "\t".join(recovery_job.EXPECTED_COLUMNS) + "\n"
    rows = []
    for number in (1, 2):
        body = f"Subject: Article {number}\n\nBody\n".encode()
        path = directory / f"alt.games.everquest-pre2000-{number:06d}.txt"
        path.write_bytes(body)
        rows.append(f"{number}\t<{number}@example.net>\t1999-01-01\t"
                    f"{hashlib.sha256(body).hexdigest()}\t{len(body)}\tyes\n")
    (directory / "manifest.tsv").write_text(header + "".join(rows))
    assert len(recovery_job.verified_paths(tmp_path)) == 2
    (directory / "alt.games.everquest-pre2000-000002.txt").write_bytes(b"changed")
    with pytest.raises(ValueError, match="differs from manifest"):
        recovery_job.verified_paths(tmp_path)


def test_index_paths_skips_existing_and_creates_full_document(tmp_path):
    directory = tmp_path / recovery_job.RELATIVE_DIR
    directory.mkdir(parents=True)
    paths = []
    for number in (1, 2):
        path = directory / f"alt.games.everquest-pre2000-{number:06d}.txt"
        path.write_text("body")
        paths.append(path)
    existing_id = paths[0].relative_to(tmp_path).as_posix()
    new_id = paths[1].relative_to(tmp_path).as_posix()
    client = MagicMock()
    client.exists.side_effect = lambda index, id: id == existing_id
    client.index.return_value = {"result": "created"}
    es_manager = MagicMock()
    es_manager._index_name = "eq-archive"
    es_manager.get_client.return_value = client
    indexer = MagicMock()
    indexer._get_docs.return_value = [{"id": new_id, "text_full": "full source", "text": [{"vector": [0] * 768}]}]

    assert recovery_job.index_paths(tmp_path, paths, indexer, es_manager) == (1, 1)
    client.index.assert_called_once()
    doc = client.index.call_args.kwargs["document"]
    assert doc["text_full"] == "full source"
    assert doc["domain_name"] == "archive.usenet.blueworldhosting.com"
    assert client.index.call_args.kwargs["op_type"] == "create"


def test_index_paths_fails_if_document_is_empty(tmp_path):
    path = tmp_path / "article.txt"
    path.write_text("body")
    es_manager = MagicMock()
    es_manager._index_name = "eq-archive"
    es_manager.get_client.return_value.exists.return_value = False
    indexer = MagicMock()
    indexer._get_docs.return_value = []
    with pytest.raises(ValueError, match="unexpected generated documents"):
        recovery_job.index_paths(tmp_path, [path], indexer, es_manager)
    es_manager.get_client.return_value.index.assert_not_called()


def test_recovery_connection_does_not_change_existing_index_settings():
    with patch("indexer.es_manager.Elasticsearch") as factory:
        client = factory.return_value
        client.ping.return_value = True
        manager = ElasticsearchManager(host="http://elasticsearch", port="9200",
                                       manage_index=False)
        assert manager.get_client() is client
        client.indices.put_index_template.assert_not_called()
        client.indices.create.assert_not_called()
