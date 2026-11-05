"""Identical requests save inference while preserving all source occurrences."""
from dataclasses import replace

import numpy as np
import pytest
from langchain_core.documents import Document

from indexer import gemma, passages
from indexer.openai_manager import OpenAIManager


def repeated_plan(contexts=('Post by Healer','Post by Healer','Post by Other','Post by Healer')):
    body='Repeated archival testimony.\n\n'
    source=body*len(contexts)
    parts=tuple(passages.Passage(i*len(body),(i+1)*len(body),body,'Epic help',context,'post')
                for i,context in enumerate(contexts))
    plan=passages.Plan(passages.digest(source),None,source,'Epic help','source_text',(),parts)
    passages.verify(source,plan)
    return source,plan


def manager(monkeypatch, values=None):
    monkeypatch.setenv('GEMMA_EMBEDDING_PROFILE','cuda')
    monkeypatch.setenv('GEMMA_EMBEDDING_BATCH_SIZE','2')
    service=OpenAIManager.__new__(OpenAIManager);service._embedding_model_name=gemma.MODEL
    seen=[]
    def embed(inputs):
        seen.extend(inputs)
        return values if values is not None else [[float(i+1)]+[0.]*767 for i in range(len(inputs))]
    service._embed_nomic_batch=embed
    return service,seen


def test_repeated_requests_are_embedded_once_but_every_offset_and_vector_is_returned(monkeypatch):
    source,plan=repeated_plan();service,seen=manager(monkeypatch)
    rows=service.get_passages_and_embeddings(Document(page_content=source),plan)
    assert len(seen)==2 and len(set(seen))==2
    assert len(rows)==4
    assert [(r['source_start'],r['source_end']) for r in rows]==[(p.start,p.end) for p in plan.passages]
    assert all(r['text_chunk']==source[r['source_start']:r['source_end']] for r in rows)
    assert np.array_equal(rows[0]['vector'],rows[1]['vector'])
    assert not np.shares_memory(rows[0]['vector'],rows[1]['vector'])
    assert not np.array_equal(rows[0]['vector'],rows[2]['vector'])


def test_title_changes_and_context_changes_are_distinct_embedding_inputs(monkeypatch):
    source,plan=repeated_plan()
    plan=replace(plan,passages=tuple(replace(p,title='Different title') if i==1 else p for i,p in enumerate(plan.passages)))
    service,seen=manager(monkeypatch)
    rows=service.get_passages_and_embeddings(Document(page_content=source),plan)
    assert len(seen)==3 and len(rows)==4
    assert any('Different title' in text for text in seen)
    assert any('Post by Other' in text for text in seen)


@pytest.mark.parametrize('vectors',[[[1.]*767],[[float('nan')]*768],[]])
def test_failed_unique_vector_batch_never_returns_a_partially_expanded_document(monkeypatch,vectors):
    source,plan=repeated_plan(('same','same'));service,_=manager(monkeypatch,vectors)
    with pytest.raises(ValueError):service.get_passages_and_embeddings(Document(page_content=source),plan)
    assert not hasattr(service,'passage_provenance')


def test_reuse_does_not_cross_plan_or_model_calls(monkeypatch):
    source,plan=repeated_plan(('same','same'));service,seen=manager(monkeypatch)
    for _ in range(2):service.get_passages_and_embeddings(Document(page_content=source),plan)
    assert len(seen)==2


def test_enabled_persistent_cache_reuses_requests_across_independent_managers_and_plans(tmp_path,monkeypatch):
    monkeypatch.setenv('EMBEDDING_CACHE_PATH',str(tmp_path/'shared.sqlite'))
    monkeypatch.setenv('GEMMA_EMBEDDING_PROFILE','cuda');monkeypatch.setenv('GEMMA_EMBEDDING_BATCH_SIZE','8')
    source,plan=repeated_plan();calls=[];results=[]
    for _ in range(2):
        service=OpenAIManager.__new__(OpenAIManager);service._embedding_model_name=gemma.MODEL
        service._fetch_nomic_batch=lambda inputs:calls.extend(inputs) or [[1.]+[0.]*767 for _ in inputs]
        results.append(service.get_passages_and_embeddings(Document(page_content=source),plan))
        service._embedding_cache.close()
    assert len(calls)==2 and len(results[0])==len(results[1])==4
    assert [(p['source_start'],p['source_end']) for p in results[1]]==[(p.start,p.end) for p in plan.passages]
    assert np.array_equal(results[0][0]['vector'],results[1][0]['vector'])
