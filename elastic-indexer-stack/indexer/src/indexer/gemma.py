"""Pinned EmbeddingGemma 2 text contract, shared by backfill and new imports.

The canonical model version describes the embedding space, not the GPU/quant.
Keep the source-preserving 480-token policy; count with Gemma's own tokenizer.
"""
import argparse
from functools import lru_cache
import hashlib
import math
import os
from pathlib import Path
import urllib.request

from tokenizers import Tokenizer
from .chunking import chunk_source as source_chunks

MODEL = "embeddinggemma-2-768-v1"
CHUNKING_VERSION = "embeddinggemma2-paragraphs-480-overlap48-v1"
DOCUMENT_PREFIX = "title: none | text: "
QUERY_PREFIX = "task: search result | query: "
TOKENIZER_REVISION = "f9972ac22e696a105c482202646979e33e4b5a1e"
TOKENIZER_URL = f"https://huggingface.co/google/embeddinggemma-2/resolve/{TOKENIZER_REVISION}/tokenizer.json"
TOKENIZER_SHA256 = "4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4"
DEFAULT_TOKENIZER_PATH = "/opt/eqarchives/gemma-tokenizer.json"


def download_tokenizer(path):
    with urllib.request.urlopen(TOKENIZER_URL, timeout=120) as response:
        raw = response.read()
    if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("Gemma tokenizer checksum mismatch")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


@lru_cache(maxsize=1)
def load_tokenizer():
    raw = Path(os.environ.get("GEMMA_TOKENIZER_PATH", DEFAULT_TOKENIZER_PATH)).read_bytes()
    if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("Gemma tokenizer checksum mismatch")
    tokenizer = Tokenizer.from_str(raw.decode("utf-8"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def chunk_source(text, tokenizer=None, max_tokens=480, overlap_tokens=48):
    return source_chunks(text, tokenizer or load_tokenizer(), max_tokens, overlap_tokens, prefix=DOCUMENT_PREFIX)


def vector(values):
    if (len(values) != 768 or any(type(x) not in (float, int) or not math.isfinite(x) for x in values)
            or not any(values)):
        raise ValueError("Expected a finite nonzero 768-dimensional vector")
    return values


def mean_vector(vectors):
    if not vectors:
        raise ValueError("Cannot embed empty text")
    for values in vectors:
        vector(values)
    mean = [sum(row[i] for row in vectors) / len(vectors) for i in range(768)]
    norm = math.sqrt(sum(x*x for x in mean))
    if not norm or not math.isfinite(norm):
        raise ValueError("Invalid aggregate vector")
    return [x / norm for x in mean]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-tokenizer", type=Path, required=True)
    download_tokenizer(parser.parse_args().download_tokenizer)
