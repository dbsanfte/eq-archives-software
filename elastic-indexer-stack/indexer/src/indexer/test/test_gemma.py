import hashlib
import io
import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from langchain_core.documents import Document

from indexer import gemma
from indexer.openai_manager import OpenAIManager
from indexer.reindex_preparation import verify_prepared

V = [1.] + [0.] * 767


def test_real_pinned_tokenizer_preserves_unicode_long_words_and_all_offsets():
    text = 'Cleric spell guide.\r\n\r\n' + ('resurrection 🧝 聖職者 ' * 700) + '\n' + 'q' * 2400
    tokenizer = gemma.load_tokenizer()
    chunks = gemma.chunk_source(text)
    verify_prepared(text, chunks)
    assert len(chunks) > 3
    for chunk in chunks:
        assert len(tokenizer.encode(gemma.DOCUMENT_PREFIX + chunk.text).ids) <= 480
    assert gemma.chunk_source('') == []


def test_tokenizer_download_and_cache_require_checksum(tmp_path, monkeypatch):
    raw = b'fixture tokenizer'
    monkeypatch.setattr(gemma, 'TOKENIZER_SHA256', hashlib.sha256(raw).hexdigest())
    target = tmp_path / 'nested/tokenizer.json'
    with patch.object(gemma.urllib.request, 'urlopen', return_value=io.BytesIO(raw)):
        gemma.download_tokenizer(target)
    assert target.read_bytes() == raw
    with patch.object(gemma.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupt')):
        with pytest.raises(ValueError, match='checksum'):
            gemma.download_tokenizer(target)
    monkeypatch.setenv('GEMMA_TOKENIZER_PATH', str(target))
    gemma.load_tokenizer.cache_clear()
    target.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='checksum'):
        gemma.load_tokenizer()
    gemma.load_tokenizer.cache_clear()


@pytest.mark.parametrize('bad', [[0.] * 768, [1.], [True] + [0.] * 767, [float('nan')] * 768])
def test_invalid_vectors_never_become_a_checkpoint(bad):
    with pytest.raises(ValueError):
        gemma.vector(bad)


def test_mean_normalizes_and_checks_all_vectors():
    assert gemma.mean_vector([V, V]) == V
    with pytest.raises(ValueError):
        gemma.mean_vector([])
    with pytest.raises(ValueError):
        gemma.mean_vector([V, [-x for x in V]])


def manager():
    client = object.__new__(OpenAIManager)
    client._embedding_model_name = gemma.MODEL
    client._logger = MagicMock()
    client._openai_embeddings = MagicMock()
    return client


def test_gemma_uses_source_chunks_batched_prefixes_and_normalized_summary(monkeypatch):
    monkeypatch.setenv('GEMMA_EMBEDDING_PROFILE', 'cuda')
    monkeypatch.setenv('GEMMA_EMBEDDING_BATCH_SIZE', '2')
    client = manager()
    def respond(**kwargs):
        assert kwargs['model'] == gemma.MODEL
        assert all(text.startswith(gemma.DOCUMENT_PREFIX) for text in kwargs['input'])
        return {'model': gemma.MODEL, 'data': [{'index': i, 'embedding': V} for i in reversed(range(len(kwargs['input'])))]}
    client._openai_embeddings.client.create.side_effect = respond
    body = 'Guild recruitment. ' * 700
    result = client.get_chunks_and_embeddings(Document(page_content=body))
    assert len(result) >= 3 and client.chunking_version == gemma.CHUNKING_VERSION
    assert result[0]['text_chunk'] == body[:len(result[0]['text_chunk'])]
    assert client.embed_text(body).tolist() == V
    assert client.embed_text(' ') is None
    client._openai_embeddings.embed_query.assert_not_called()
    assert len(client.get_chunks_and_embeddings(Document(page_content=''))) == 0


def test_gemma_rejects_invalid_capacity_incomplete_responses_and_prepared_sources(monkeypatch):
    client = manager()
    monkeypatch.setenv('GEMMA_EMBEDDING_BATCH_SIZE', '3')
    monkeypatch.setenv('GEMMA_EMBEDDING_PROFILE', 'vulkan')
    with pytest.raises(ValueError, match='capacity'):
        client.get_chunks_and_embeddings(Document(page_content='Cleric'))
    monkeypatch.setenv('GEMMA_EMBEDDING_BATCH_SIZE', '1')
    client._openai_embeddings.client.create.return_value = {'data': []}
    with pytest.raises(ValueError, match='incomplete'):
        client.embed_text('Cleric')
    with pytest.raises(ValueError, match='omit'):
        client.get_chunks_and_embeddings(Document(page_content='Cleric'), _prepared_source=[])


def test_server_model_mismatch_never_produces_ready_vectors(monkeypatch):
    client = manager()
    client._openai_embeddings.client.create.return_value = {'model': 'old-nomic', 'data': [{'index': 0, 'embedding': V}]}
    with pytest.raises(ValueError, match='different model'):
        client.embed_text('Cleric')
    monkeypatch.setenv('GEMMA_EMBEDDING_PROFILE', 'typo')
    with pytest.raises(ValueError, match='profile'):
        client.embed_text('Cleric')
