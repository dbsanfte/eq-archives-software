from unittest.mock import patch
import logging

import numpy as np
import pytest
from tokenizers import Tokenizer

from indexer.chunking import DOCUMENT_PREFIX, chunk_source, load_tokenizer
from indexer.openai_manager import OpenAIManager


@pytest.mark.parametrize("text", [
    "",
    " \t\r\n ",
    "# Seru\r\n\r\n" + ("Prepare the Serubane weapons for battle.\r\n" * 150) + "Final line.\r\n",
    "# Equipment\n\n" + ("Sword damage and weapon delay. " * 500),
    "古い武器と戦い。" * 500,
    "https://example.org/" + "path-" * 500,
    "x" * 5000,
], ids=["empty", "whitespace", "line-endings", "long-paragraph", "unicode", "url", "long-word"])
def test_chunks_cover_exact_source_and_actual_prefixed_request_budget(text):
    tokenizer = load_tokenizer()
    chunks = chunk_source(text, tokenizer)
    rebuilt = ""
    covered = 0
    for chunk in chunks:
        assert chunk.start <= covered < chunk.end
        assert chunk.text == text[chunk.start:chunk.end]
        rebuilt += text[covered:chunk.end]
        covered = chunk.end
        assert len(tokenizer.encode(DOCUMENT_PREFIX + chunk.text).ids) <= 480
    assert rebuilt == text
    if len(chunks) > 1 and " " in text:
        assert any(right.start < left.end for left, right in zip(chunks, chunks[1:]))


def test_paragraph_boundaries_and_overlap_retain_heading_context():
    text = "# Serubane weapons\n\n" + "weapon damage " * 150 + "\n\n# Lord Seru\n\n" + "battle tactics " * 200
    chunks = chunk_source(text)
    assert len(chunks) > 1
    assert any(chunk.text.endswith("\n\n") for chunk in chunks[:-1])
    assert "# Lord Seru" in "".join(chunk.text for chunk in chunks)
    assert all(chunk.start < chunk.end for chunk in chunks)


@pytest.mark.parametrize("word", ["x" * 5000, "qwertyuiopasdfghjklzxcvbnm0123456789" * 100],
                         ids=["repeated-character", "encoded-string"])
def test_long_words_fit_server_wordpiece_budget_without_unknown_word_shortcut(word):
    text = "# Archived message\n\nEncoded source: " + word + "\n\nFinal line."
    # llama.cpp does not use Hugging Face WordPiece's 100-character word cap.
    # An independent tokenizer without that shortcut reproduces the server's
    # expansion of encoded strings that previously caused HTTP 500 responses.
    server_tokenizer = Tokenizer.from_str(load_tokenizer().to_str())
    server_tokenizer.model.max_input_chars_per_word = len(text)
    chunks = chunk_source(text)
    covered = 0
    rebuilt = ""
    for chunk in chunks:
        assert chunk.start <= covered < chunk.end
        assert len(server_tokenizer.encode(DOCUMENT_PREFIX + chunk.text).ids) <= 480
        rebuilt += text[covered:chunk.end]
        covered = chunk.end
    assert rebuilt == text


def test_unknown_multibyte_words_preserve_every_source_character():
    text = "unknown\U0001f9ed" * 700
    chunks = chunk_source(text)
    assert len(chunks) > 1
    covered = 0
    rebuilt = ""
    for chunk in chunks:
        assert chunk.start <= covered < chunk.end
        assert chunk.text == text[chunk.start:chunk.end]
        # Conservatively bounded unknown spans also bound their UTF-8 bytes.
        assert len(chunk.text.encode("utf-8")) + len(load_tokenizer().encode(DOCUMENT_PREFIX).ids) <= 480
        rebuilt += text[covered:chunk.end]
        covered = chunk.end
    assert rebuilt == text


@pytest.mark.parametrize("max_tokens,overlap", [(8,0), (480,-1), (480,300)])
def test_bad_budgets_are_rejected(max_tokens, overlap):
    with pytest.raises(ValueError, match="budget"):
        chunk_source("source", max_tokens=max_tokens, overlap_tokens=overlap)


def test_nomic_embeds_once_per_chunk_with_document_prefix_and_no_boundary_embeddings():
    manager = OpenAIManager.__new__(OpenAIManager)
    manager._embedding_model_name = "text-embedding-nomic-embed-text-v1.5@q8_0"
    text = "Serubane weapon damage. " * 500
    with patch.object(manager, "embed_text", return_value=np.zeros(768)) as embed:
        chunks = manager.get_chunks_and_embeddings(type("Document", (), {"page_content": text})())
    assert len(chunks) == len(chunk_source(text)) == embed.call_count
    assert [call.args[0] for call in embed.call_args_list] == [DOCUMENT_PREFIX + chunk["text_chunk"] for chunk in chunks]


@pytest.mark.parametrize("vector", [[0] * 767, [float("nan")] * 768])
def test_invalid_embeddings_are_rejected(vector):
    manager = OpenAIManager.__new__(OpenAIManager)
    manager._embedding_model_name = "nomic-embed-text-v1.5"
    with patch.object(manager, "embed_text", return_value=vector), pytest.raises(ValueError, match="768 finite"):
        manager.get_chunks_and_embeddings(type("Document", (), {"page_content": "source"})())


def test_tokenizer_checksum_is_verified(tmp_path, monkeypatch):
    path = tmp_path / "tokenizer.json"
    path.write_text("changed tokenizer")
    load_tokenizer.cache_clear()
    monkeypatch.setenv("NOMIC_TOKENIZER_PATH", str(path))
    with pytest.raises(ValueError, match="checksum"):
        load_tokenizer()
    load_tokenizer.cache_clear()


def test_embedding_failures_do_not_log_upstream_payloads(caplog):
    manager = OpenAIManager.__new__(OpenAIManager)
    manager._logger = logging.getLogger("embedding-regression")
    manager._openai_embeddings = type("Client", (), {
        "embed_query": lambda _, text: (_ for _ in ()).throw(RuntimeError("secret-bearing upstream payload"))
    })()
    with pytest.raises(RuntimeError):
        manager.embed_text("source")
    assert "RuntimeError" in caplog.text
    assert "secret-bearing" not in caplog.text
