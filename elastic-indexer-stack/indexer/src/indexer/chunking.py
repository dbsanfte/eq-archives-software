"""Source-preserving Nomic chunks bounded by the actual WordPiece tokenizer."""

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import urllib.request

from tokenizers import Tokenizer


CHUNKING_VERSION = "nomic-paragraphs-480-overlap48-v1"
DOCUMENT_PREFIX = "search_document: "
TOKENIZER_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"
TOKENIZER_URL = f"https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/resolve/{TOKENIZER_REVISION}/tokenizer.json"
TOKENIZER_SHA256 = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"
DEFAULT_TOKENIZER_PATH = "/opt/eqarchives/nomic-tokenizer.json"


def download_tokenizer(path):
    path = Path(path)
    with urllib.request.urlopen(TOKENIZER_URL, timeout=120) as response:
        raw = response.read()
    if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("Nomic tokenizer checksum mismatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


@lru_cache(maxsize=1)
def load_tokenizer():
    path = Path(os.environ.get("NOMIC_TOKENIZER_PATH", DEFAULT_TOKENIZER_PATH))
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("Nomic tokenizer checksum mismatch")
    tokenizer = Tokenizer.from_str(raw.decode("utf-8"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


@dataclass(frozen=True)
class SourceChunk:
    start: int
    end: int
    text: str


def chunk_source(text, tokenizer=None, max_tokens=480, overlap_tokens=48):
    """Prefer paragraphs/headings, then lines/sentences, then exact token spans.

    Offsets refer to the original source, including whitespace and line endings.
    The prefix and special tokens count toward the budget. Overlap adds context
    without truncating the source or embedding sentences to choose boundaries.
    """
    tokenizer = tokenizer or load_tokenizer()
    prefix_tokens = len(tokenizer.encode(DOCUMENT_PREFIX).ids)
    budget = max_tokens - prefix_tokens
    if budget < 8 or not 0 <= overlap_tokens < budget // 2:
        raise ValueError("invalid chunk token budget or overlap")
    if not text:
        return []
    encoding = tokenizer.encode(text, add_special_tokens=False)
    offsets = encoding.offsets
    if not offsets:
        return [SourceChunk(0, len(text), text)]
    starts = [offset[0] for offset in offsets]
    paragraphs = [match.end() for match in re.finditer(r"\r?\n[ \t]*\r?\n", text)]
    lines = [match.end() for match in re.finditer(r"\r?\n|[.!?][ \t]+", text)]
    words = [match.end() for match in re.finditer(r"\s+", text)]
    chunks = []
    start = 0
    while start < len(text):
        first = bisect_left(starts, start)
        last = min(first + budget, len(offsets))
        end = starts[last] if last < len(offsets) else len(text)
        if end < len(text):
            minimum = starts[min(first + budget // 2, len(offsets) - 1)]
            for boundaries in (paragraphs, lines, words):
                position = bisect_right(boundaries, end) - 1
                if position >= 0 and boundaries[position] >= minimum:
                    end = boundaries[position]
                    break
        # Retokenizing a boundary can change WordPiece segmentation. Always
        # validate the actual request; reduce it rather than relying on estimates.
        while len(tokenizer.encode(DOCUMENT_PREFIX + text[start:end]).ids) > max_tokens:
            last = bisect_left(starts, end) - 1
            if last <= first:
                raise ValueError("cannot fit source token within embedding context")
            end = starts[last]
        if end <= start:
            raise ValueError("chunker did not advance")
        chunks.append(SourceChunk(start, end, text[start:end]))
        if end == len(text):
            break
        last = bisect_left(starts, end)
        overlap = max(first + 1, last - overlap_tokens)
        next_start = starts[overlap] if overlap < len(starts) else end
        # Begin at a word boundary where possible, without creating a source gap.
        position = bisect_left(words, next_start)
        if position < len(words) and words[position] < end:
            next_start = words[position]
        start = min(end, max(start + 1, next_start))
    return chunks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-tokenizer", type=Path, required=True)
    download_tokenizer(parser.parse_args().download_tokenizer)
