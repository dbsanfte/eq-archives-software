"""CPU source preparation isolated from embedding and Elasticsearch clients."""
from functools import lru_cache

from .archive_handler import ArchiveHandler
from .chunking import chunk_source
from .text_handler import TextHandler


@lru_cache(maxsize=1)
def handler():
    return TextHandler(ArchiveHandler(), None, llm_enrichment_enabled=False)


def prepare_source(record_id, path, available, original):
    # No network clients, writes or LLM enrichment in preparation processes.
    try:
        text = original
        if available:
            documents = handler()._get_documents_from_file(record_id, path)
            if len(documents) != 1:
                raise ValueError('expected one complete source document')
            text = documents[0].page_content
        return text, chunk_source(text)
    except Exception as error:
        raise ValueError(f'Source preparation failed ({type(error).__name__})') from None


def verify_prepared(text, chunks):
    end = previous_start = 0
    for i, chunk in enumerate(chunks):
        if (not 0 <= chunk.start < chunk.end <= len(text) or
                chunk.start > end or i and chunk.start <= previous_start or
                text[chunk.start:chunk.end] != chunk.text):
            raise ValueError('Prepared chunks differ from the complete source')
        previous_start, end = chunk.start, chunk.end
    if chunks and end != len(text) or text.strip() and not chunks:
        raise ValueError('Prepared chunks omit source content')
