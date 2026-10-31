"""Nomic batches preserve source coverage and bind each vector to its input."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest
from langchain_core.documents import Document

from indexer.chunking import CHUNKING_VERSION, DOCUMENT_PREFIX, chunk_source
from indexer.openai_manager import OpenAIManager


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setenv("NOMIC_EMBEDDING_BATCH_SIZE", "2")
    manager = OpenAIManager.__new__(OpenAIManager)
    manager._embedding_model_name = "text-embedding-nomic-embed-text-v1.5@q8_0"
    manager._logger = logging.getLogger("batch-regression")
    manager._openai_embeddings = Mock()
    return manager


def vector(number):
    values = [0.0] * 768
    values[number] = 1.0
    return values


def test_batching_preserves_every_chunk_prefix_vector_and_final_partial_batch(manager):
    # Three distinct near-limit paragraphs exercise two inputs plus a final one.
    text = "\n\n".join((word + " ") * 330 for word in ("cleric", "guild", "dragon"))
    source = chunk_source(text)
    assert len(source) >= 3 and len(source) % 2 == 1
    expected = {DOCUMENT_PREFIX + chunk.text: vector(i) for i, chunk in enumerate(source)}

    def respond(*, input, model, encoding_format):
        assert model == manager._embedding_model_name and encoding_format == "float"
        # Index, not response arrival order, determines which chunk owns a vector.
        rows = [{"index": i, "embedding": expected[value]} for i, value in enumerate(input)]
        return SimpleNamespace(model_dump=lambda: {"data": list(reversed(rows))})

    manager._openai_embeddings.client.create.side_effect = respond
    with patch("time.sleep") as sleep:
        result = manager.get_chunks_and_embeddings(Document(page_content=text))
    calls = manager._openai_embeddings.client.create.call_args_list
    assert [len(call.kwargs["input"]) for call in calls] == [2] * (len(source) // 2) + [1]
    assert [value for call in calls for value in call.kwargs["input"]] == list(expected)
    assert [item["text_chunk"] for item in result] == [chunk.text for chunk in source]
    for item in result:
        np.testing.assert_array_equal(item["vector"], expected[DOCUMENT_PREFIX + item["text_chunk"]])
    assert manager.chunking_version == CHUNKING_VERSION
    manager._openai_embeddings.embed_query.assert_not_called()
    sleep.assert_not_called()


@pytest.mark.parametrize("rows", [
    [],
    [{"index": 0, "embedding": vector(0)}] * 2,
    [{"index": 1, "embedding": vector(0)}],
    [{"index": -1, "embedding": vector(0)}],
    [{"index": False, "embedding": vector(0)}],
    [{"index": "0", "embedding": vector(0)}],
    [{"embedding": vector(0)}],
    [{"index": 0}],
    [{"index": 0, "embedding": [0.0] * 767}],
    [{"index": 0, "embedding": [[0.0]] * 768}],
    [{"index": 0, "embedding": [float("nan")] * 768}],
    [{"index": 0, "embedding": [float("inf")] * 768}],
], ids=["missing", "extra", "wrong-index", "negative", "boolean", "string-index", "missing-index",
        "missing-vector", "wrong-dimensions", "nested-vector", "nan", "infinity"])
def test_invalid_batch_cannot_return_a_document_with_wrong_or_missing_vectors(manager, rows):
    manager._openai_embeddings.client.create.return_value = {"data": rows}
    with pytest.raises(ValueError):
        manager.get_chunks_and_embeddings(Document(page_content="Cleric spells."))


def test_duplicate_indices_are_rejected_even_when_response_count_matches(manager):
    manager._openai_embeddings.client.create.return_value = {
        "data": [{"index": 0, "embedding": vector(0)}] * 2}
    with pytest.raises(ValueError, match="indices"):
        manager.get_chunks_and_embeddings(Document(page_content="cleric " * 600))


def test_later_batch_failure_does_not_return_partial_source(manager, caplog):
    manager._openai_embeddings.client.create.side_effect = [
        {"data": [{"index": i, "embedding": vector(i)} for i in range(2)]},
        RuntimeError("secret-bearing upstream payload"),
    ]
    with pytest.raises(RuntimeError):
        manager.get_chunks_and_embeddings(Document(page_content="cleric " * 1200))
    assert manager._openai_embeddings.client.create.call_count == 2
    assert "RuntimeError" in caplog.text and "secret-bearing" not in caplog.text


@pytest.mark.parametrize("value", ["0", "3", "-1", "abc", "1.5"])
def test_unsupported_batch_size_fails_before_inference(manager, monkeypatch, value):
    monkeypatch.setenv("NOMIC_EMBEDDING_BATCH_SIZE", value)
    with pytest.raises(ValueError, match="NOMIC_EMBEDDING_BATCH_SIZE"):
        manager.get_chunks_and_embeddings(Document(page_content="Cleric spells."))
    manager._openai_embeddings.client.create.assert_not_called()
    manager._openai_embeddings.embed_query.assert_not_called()


def test_empty_source_never_calls_embedding_service(manager):
    assert manager.get_chunks_and_embeddings(Document(page_content="")) == []
    manager._openai_embeddings.client.create.assert_not_called()


def test_cuda_opt_in_larger_batches_keep_all_source_chunks(manager, monkeypatch):
    monkeypatch.setenv('NOMIC_EMBEDDING_PROFILE', 'cuda')
    monkeypatch.setenv('NOMIC_EMBEDDING_BATCH_SIZE', '8')
    text = 'cleric spells and guild raids. ' * 3000
    source = chunk_source(text)
    manager._openai_embeddings.client.create.side_effect = lambda **kw: {
        'data': [{'index': i, 'embedding': vector(i)} for i in range(len(kw['input']))]}
    result = manager.get_chunks_and_embeddings(Document(page_content=text))
    assert [row['text_chunk'] for row in result] == [chunk.text for chunk in source]
    assert max(len(call.kwargs['input']) for call in manager._openai_embeddings.client.create.call_args_list) == 8


def test_each_backend_retains_its_own_capacity_and_encoding(manager, monkeypatch):
    import base64
    import struct
    monkeypatch.setenv('NOMIC_EMBEDDING_PROFILE', 'cuda')
    monkeypatch.setenv('NOMIC_EMBEDDING_BATCH_SIZE', '8')
    monkeypatch.setenv('NOMIC_EMBEDDING_ENCODING', 'base64')
    expected = vector(7)
    manager._openai_embeddings.client.create.return_value = {'data': [
        {'index': 0, 'embedding': base64.b64encode(struct.pack('<768f', *expected)).decode()}]}
    result = manager.get_chunks_and_embeddings(Document(page_content='cleric'))
    np.testing.assert_array_equal(result[0]['vector'], expected)
    assert manager._openai_embeddings.client.create.call_args.kwargs['encoding_format'] == 'base64'
    manager._nomic_profile, manager._nomic_batch_size, manager._nomic_encoding = 'vulkan', '2', 'float'
    manager._openai_embeddings.client.create.return_value = {'data': [{'index': 0, 'embedding': expected}]}
    result = manager.get_chunks_and_embeddings(Document(page_content='cleric'))
    np.testing.assert_array_equal(result[0]['vector'], expected)
    assert manager._openai_embeddings.client.create.call_args.kwargs['encoding_format'] == 'float'


@pytest.mark.parametrize('payload', ['invalid base64!', 'AAAA', [0.0]*768])
def test_bad_base64_cannot_return_partial_document(manager, monkeypatch, payload):
    monkeypatch.setenv('NOMIC_EMBEDDING_ENCODING', 'base64')
    manager._openai_embeddings.client.create.return_value = {'data': [{'index': 0, 'embedding': payload}]}
    with pytest.raises(ValueError):
        manager.get_chunks_and_embeddings(Document(page_content='cleric'))


@pytest.mark.parametrize('name,value', [('NOMIC_EMBEDDING_PROFILE', 'other'), ('NOMIC_EMBEDDING_ENCODING', 'other')])
def test_unknown_profile_or_encoding_rejected_before_request(manager, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        manager.get_chunks_and_embeddings(Document(page_content='cleric'))
    manager._openai_embeddings.client.create.assert_not_called()
