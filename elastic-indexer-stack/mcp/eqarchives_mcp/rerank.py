"""Shared bounded candidate scoring. Source text and model credentials stay server-side."""
import asyncio
from collections import OrderedDict
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import time

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from .passages import evidence_query, evidence_rows, select_evidence, FALLBACK_FIELDS, fallback_query
from .ranking_policy import PASSAGE_MODE

MODEL = "qwen3-reranker-0.6b-q8-v1"
TOKENIZER_SHA = "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
FIELDS = ["title", "text.text_chunk", "text.title", "text.context", "text_full", "llm_image_text_full"]
PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. '
          'Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
          '<Instruct>: Given a web search query, retrieve relevant passages that answer the query\n<Query>: ')
SUFFIX = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
STOP = set('a an the is are was were of for to in on and or do does did how what why when can with'.split())
logger = logging.getLogger(__name__)


def passage(query, source, tokenizer):
    rows = source.get("text") or []
    if not isinstance(rows, list) or len(rows) > 4096:
        return None
    rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("text_chunk"), str) and r["text_chunk"].strip()]
    if not rows:
        body = source.get("text_full") or source.get("llm_image_text_full")
        if not isinstance(body, str) or not body.strip():
            return None
        rows = [{"text_chunk": body}]
    def terms(value):
        return set(re.findall(r"[^\W_]+", value.lower())) - STOP
    wanted = terms(query)
    row = max(rows, key=lambda r: sum(len(w) for w in wanted & terms(r["text_chunk"][:8192])))
    title, context = row.get("title") or source.get("title"), row.get("context")
    heading = "\n".join(v for v in (title, context) if isinstance(v, str) and v.strip())
    heading = tokenizer.decode(tokenizer.encode(heading[:2048], add_special_tokens=False).ids[:96])
    heading = heading + "\n\n" if heading else ""
    allowance = 768 - len(tokenizer.encode(PREFIX + query + "\n<Document>: " + heading + SUFFIX, add_special_tokens=False).ids) - 8
    if allowance < 32:
        return None
    original = row["text_chunk"][:8192]
    tokens = tokenizer.encode(original, add_special_tokens=False).ids
    document = heading + (original if len(tokens) <= allowance else tokenizer.decode(tokens[:allowance]))
    if len(tokenizer.encode(PREFIX + query + "\n<Document>: " + document + SUFFIX, add_special_tokens=False).ids) > 768:
        return None
    return document


def scores(data, count):
    if not isinstance(data, dict) or data.get("model") != MODEL:
        raise ValueError("Model identity")
    rows = data.get("results")
    if not isinstance(rows, list) or len(rows) != count:
        raise ValueError("Incomplete scores")
    values = [None] * count
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid result")
        n, score = row.get("index"), row.get("relevance_score")
        if (type(n) is not int or not 0 <= n < count or values[n] is not None or
                type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError("Invalid score")
        values[n] = score
    return values


class Reranker:
    def __init__(self, client, tokenizer):
        self.client, self.tokenizer = client, tokenizer
        self.lock = asyncio.Semaphore(2)
        self.batch_slots = asyncio.Semaphore(4)
        self.pending = {}
        self.cache = OrderedDict()
        self.cooldown = 0.0
        self.deadline = 3.0
        self.passage_mode = PASSAGE_MODE

    @classmethod
    def connect(cls):
        url = os.getenv("RERANK_URL")
        if not url:
            return None
        # Operator endpoint only; never caller-controlled or proxy-environment controlled.
        tokenizer_path = Path(os.getenv("RERANK_TOKENIZER", "/app/qwen-tokenizer.json"))
        if hashlib.sha256(tokenizer_path.read_bytes()).hexdigest() != TOKENIZER_SHA:
            raise ValueError("Reranker tokenizer checksum differs")
        from tokenizers import Tokenizer
        key = Path(os.getenv("RERANK_KEY_FILE", "/run/reranker/api_key")).read_text().strip()
        if not key:
            raise ValueError("Reranker credential is empty")
        return cls(httpx.AsyncClient(base_url=url.rstrip("/") + "/", headers={"Authorization": "Bearer " + key},
                   trust_env=False, follow_redirects=False, timeout=2.5, limits=httpx.Limits(max_connections=4)),
                   Tokenizer.from_file(str(tokenizer_path)))

    async def close(self):
        await self.client.aclose()

    async def prepare(self, archive, query, ids, mode=None):
        mode = mode or self.passage_mode
        if mode == 'legacy':
            data = await archive.execute({'size': len(ids), 'timeout': '1s', 'track_total_hits': False,
                '_source': FIELDS, 'query': {'ids': {'values': ids}}})
            sources = {h['_id']: h['_source'] for h in data['hits']['hits']}
            if len(sources) != len(data['hits']['hits']) or not set(sources).issubset(ids):
                raise ValueError('Unexpected passage document')
            return [(n, document) for n, id in enumerate(ids) if id in sources
                    and (document := passage(query, sources[id], self.tokenizer))]
        cached = archive.cache.get((archive.settings.model, query))
        vector = cached[1] if cached and cached[0] > time.monotonic() else None
        # Query embeddings were already obtained for hybrid retrieval. Keyword-only
        # research never starts an embedding request just to select a passage.
        data = await archive.execute(evidence_query(query, ids, archive.settings.model, vector))
        evidence = {}
        for hit in data['hits']['hits']:
            identity = hit['_id']
            if identity not in ids or identity in evidence:
                raise ValueError('Unexpected passage document')
            evidence[identity] = evidence_rows(hit)
        missing = [id for id in ids if id in evidence and not evidence[id]['passages']]
        fallback = {}
        if missing:
            data = await archive.execute(fallback_query(missing))
            for hit in data['hits']['hits']:
                identity = hit['_id']
                if identity not in missing or identity in fallback:
                    raise ValueError('Unexpected fallback document')
                fallback[identity] = hit['_source']
                evidence[identity] = evidence_rows(hit)
        inputs = []
        for n, id in enumerate(ids):
            source = evidence.get(id, {})
            rows = select_evidence(source, mode)
            documents = [passage(query, {'title': source.get('title'), 'text': [r]}, self.tokenizer) for r in rows]
            if not rows and id in fallback:
                documents = [passage(query, fallback[id], self.tokenizer)]
            inputs.extend((n, d) for d in dict.fromkeys(documents) if d)
        return inputs

    async def score(self, query, inputs):
        # Identical complete prompts share inference, but each original slot keeps
        # its own score and identity. Batch bytes and total prompts stay bounded.
        unique = list(dict.fromkeys(document for _, document in inputs))
        if len(unique) > 120:
            raise ValueError('Scoring input bound')
        batches = []
        position = 0
        while position < len(unique):
            batch = unique[position:position+20]
            while True:
                raw = json.dumps({'model': MODEL, 'query': query, 'documents': batch}, ensure_ascii=False).encode()
                if len(raw) <= 128 * 1024:
                    break
                if len(batch) < 2:
                    raise ValueError('Request too large')
                batch = batch[:len(batch)//2]
            batches.append((batch, raw))
            position += len(batch)

        async def submit(batch, raw):
            async with self.batch_slots:
                async with self.client.stream('POST', 'v1/rerank', content=raw,
                                              headers={'Content-Type': 'application/json'}) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 32768:
                            raise ValueError('Response too large')
                return dict(zip(batch, scores(json.loads(body), len(batch))))

        # The GPU schedules individual passages across its slots. Submit bounded
        # batches concurrently, shared across searches; never hold a serial
        # scoring lock or leave sibling requests running after failure/cancellation.
        tasks = [asyncio.create_task(submit(batch, raw)) for batch, raw in batches]
        try:
            parts = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        values = {document: value for part in parts for document, value in part.items()}
        by_slot = {}
        for slot, document in inputs:
            by_slot[slot] = max(by_slot.get(slot, 0), values[document])
        return by_slot

    def remember(self, key, order, status):
        prior = self.cache.get(key)
        if prior and prior[0] > time.monotonic():
            return list(prior[1]), prior[2]
        self.cache[key] = (time.monotonic() + 300, list(order), status)
        self.cache.move_to_end(key)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return list(order), status

    async def order(self, archive, query, ids, mode=None):
        mode = mode or self.passage_mode
        key = hashlib.sha256(json.dumps(['matched-passages-v1', mode, MODEL, query, ids], ensure_ascii=False).encode()).hexdigest()
        now = time.monotonic()
        cached = self.cache.get(key)
        if cached and cached[0] > now:
            self.cache.move_to_end(key)
            return list(cached[1]), cached[2]
        if key in self.pending:
            order, status = await asyncio.shield(self.pending[key])
            return list(order), status
        if self.lock.locked() or now < self.cooldown:
            return self.remember(key, ids, 'fallback')
        pending = asyncio.get_running_loop().create_future()
        self.pending[key] = pending
        order, status = list(ids), 'fallback'
        try:
            async with self.lock:
                try:
                    async with asyncio.timeout(self.deadline):
                        inputs = await self.prepare(archive, query, ids, mode)
                        if inputs:
                            values = await self.score(query, inputs)
                            ranked = sorted(values, key=lambda n: (-values[n], n))
                            for slot, original in zip(sorted(values), ranked):
                                order[slot] = ids[original]
                            status = 'applied'
                        else:
                            status = 'no_text'
                except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, ToolError):
                    self.cooldown = time.monotonic() + 15
                    logger.warning('Reranking unavailable; preserving retrieval order')
            result = self.remember(key, order, status)
            pending.set_result((tuple(result[0]), result[1]))
            return result
        finally:
            self.pending.pop(key, None)
            if not pending.done():
                # A cancelled owner must not cancel another client's coalesced
                # search. Retain its fallback for consistent subsequent pages.
                result = self.remember(key, ids, 'fallback')
                pending.set_result((tuple(result[0]), result[1]))
