#!/usr/bin/env python3
"""Rerank a saved, source-bound retrieval run; no ES writes or paid judging."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

from service import scores

ALIAS = 'qwen3-reranker-0.6b-q8-v1'
PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. '
          'Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
          '<Instruct>: Given a web search query, retrieve relevant passages that answer the query\n<Query>: ')
SUFFIX = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
STOP = set('a an the is are was were of for to in on and or do does did how what why when can with'.split())


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def terms(text):
    return set(re.findall(r'[^\W_]+', text.lower())) - STOP


def passages(query, source, tokenizer, maximum=512, count=1):
    """Select indexed passages by lexical overlap, with explicit bounded excerpts.

    This pilot selector is not a learned passage retriever. Preserve complete
    readers in the retrieval artifact and record every shortened scoring input.
    """
    if not 256 <= maximum <= 1024 or count not in (1, 2):
        raise ValueError('Invalid scoring-input limits')
    candidates = [row for row in source.get('text') or [] if isinstance(row, dict)
                  and isinstance(row.get('text_chunk'), str) and row['text_chunk'].strip()]
    method = 'indexed_passage_lexical_overlap'
    if not candidates:
        body = source.get('text_full') or source.get('llm_image_text_full') or ''
        if not isinstance(body, str) or not body.strip():
            return []
        candidates = [{'text_chunk': body}]
        method = 'reader_or_ocr_prefix'
    wanted = terms(query)
    def relevance(row):
        found = terms(row['text_chunk'])
        return sum(len(word) for word in wanted & found)
    ordered = sorted(enumerate(candidates), key=lambda pair: (-relevance(pair[1]), pair[0]))
    result, seen = [], set()
    for ordinal, row in ordered:
        title = row.get('title') or source.get('title') or ''
        context = row.get('context') or ''
        heading = '\n'.join(v for v in (title, context) if isinstance(v, str) and v.strip())
        heading = tokenizer.decode(tokenizer.encode(heading, add_special_tokens=False).ids[:96])
        heading = (heading + '\n\n') if heading else ''
        wrapper = PREFIX + query + '\n<Document>: ' + heading + SUFFIX
        allowance = maximum - len(tokenizer.encode(wrapper, add_special_tokens=False).ids) - 8
        if allowance < 32:
            raise ValueError('Query and metadata exhaust the scoring budget')
        original = row['text_chunk']
        tokens = tokenizer.encode(original, add_special_tokens=False).ids
        text = original if len(tokens) <= allowance else tokenizer.decode(tokens[:allowance])
        document = heading + text
        size = len(tokenizer.encode(PREFIX + query + '\n<Document>: ' + document + SUFFIX,
                                    add_special_tokens=False).ids)
        if size > maximum:
            raise ValueError('Scoring input exceeds the complete prompt budget')
        key = digest(document)
        if key in seen:
            continue
        seen.add(key)
        result.append({'document': document, 'tokens': size, 'input_sha256': key,
                       'source_passage_sha256': digest(original), 'passage_index': ordinal,
                       'shortened': text != original, 'selector': method,
                       'source_start': row.get('source_start'), 'source_end': row.get('source_end')})
        if len(result) == count:
            break
    return result


class Client:
    def __init__(self, url, key, timeout=90):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username
                or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('The pilot client requires a loopback HTTP endpoint')
        self.url, self.key, self.timeout = url.rstrip('/'), key, timeout

    def rank(self, query, documents):
        if not 1 <= len(documents) <= 100 or len(query) > 1000:
            raise ValueError('Invalid reranking batch')
        body = json.dumps({'model': ALIAS, 'query': query, 'documents': documents}).encode()
        if len(body) > 1024 * 1024:
            raise ValueError('Scoring request exceeds byte limit')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = urllib.request.Request(self.url + '/v1/rerank', data=body,
                   headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.key})
        start = time.perf_counter()
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                raw = response.read(128 * 1024 + 1)
                if len(raw) > 128 * 1024:
                    raise ValueError('Scoring response exceeds byte limit')
                result = json.loads(raw)
            values = scores(result, len(documents), ALIAS)
            return values, (time.perf_counter() - start) * 1000, result.get('usage', {})
        except (urllib.error.URLError, TimeoutError) as error:
            raise ValueError('Private reranker unavailable (' + type(error).__name__ + ')') from None


def rerank(hits, values):
    """Keep unsupported image/empty sources in their original slots and the tail."""
    positions = {hit['_id']: index for index, hit in enumerate(hits)}
    if (len(positions) != len(hits) or set(values) - positions.keys()
            or any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in values.values())):
        raise ValueError('Score identity differs from candidates')
    slots = [n for n, h in enumerate(hits) if h['_id'] in values]
    ordered = sorted((hits[n] for n in slots), key=lambda h: (-values[h['_id']], positions[h['_id']]))
    result = list(hits)
    for index, hit in zip(slots, ordered):
        result[index] = {**hit, 'rerank_score': values[hit['_id']]}
    return result


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--url', default='http://127.0.0.1:8083')
    parser.add_argument('--api-key-file', type=Path, required=True)
    parser.add_argument('--tokenizer', type=Path, required=True)
    parser.add_argument('--candidates', type=int, choices=(10, 20, 50), default=20)
    parser.add_argument('--passages', type=int, choices=(1, 2), default=1)
    parser.add_argument('--tokens', type=int, choices=(256, 512, 768, 1024), default=512)
    parser.add_argument('--query-ids')
    parser.add_argument('--maximum-seconds', type=int, default=1800)
    args = parser.parse_args()
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(str(args.tokenizer))
    metadata = json.loads((args.run / 'manifest.json').read_text())
    if metadata['status'] != 'complete':
        raise ValueError('Require a complete, successful retrieval snapshot')
    sources = json.loads((args.run / 'sources.json').read_text())
    queries = {q['id']: q for q in metadata['queries']}
    selected = set(args.query_ids.split(',')) if args.query_ids else set(queries)
    if not selected or selected - queries.keys() or not 1 <= args.maximum_seconds <= 3600:
        raise ValueError('Invalid query selection or deadline')
    client = Client(args.url, args.api_key_file.read_text().strip())
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    start = time.monotonic();results = []
    for line in (args.run / 'runs.jsonl').read_text().splitlines():
        baseline = json.loads(line);qid = baseline['query_id']
        if qid not in selected:
            continue
        if time.monotonic() - start > args.maximum_seconds:
            raise ValueError('Pilot deadline reached; partial receipts retained')
        hits = baseline['grouped'];inputs = []
        for hit in hits[:args.candidates]:
            source = sources[hit['_id']]
            if source['source_sha256'] != digest(source['source']):
                raise ValueError('Saved source hash changed')
            inputs.extend({'id': hit['_id'], **row} for row in passages(
                queries[qid]['text'], source['source'], tokenizer, args.tokens, args.passages))
        values, elapsed, usage = client.rank(queries[qid]['text'], [i['document'] for i in inputs]) if inputs else ([], 0, {})
        by_id = {}
        for item, score in zip(inputs, values):
            by_id[item['id']] = max(score, by_id.get(item['id'], 0))
        ranking = rerank(hits[:args.candidates], by_id) + hits[args.candidates:]
        receipt = {'query_id': qid, 'ranking': ranking, 'baseline': hits, 'milliseconds': elapsed,
                   'pairs': len(inputs), 'usage': usage, 'inputs': inputs,
                   'top1_changed': bool(hits and ranking and hits[0]['_id'] != ranking[0]['_id'])}
        save(args.out / (qid + '.json'), receipt);results.append(receipt)
        print(json.dumps({k:receipt[k] for k in ('query_id','milliseconds','pairs','top1_changed')}), flush=True)
    times = [r['milliseconds'] for r in results if r['pairs']]
    summary = {'model': ALIAS, 'candidate_limit': args.candidates, 'passages_per_candidate': args.passages,
               'tokens_per_pair': args.tokens, 'queries': len(results), 'pairs': sum(r['pairs'] for r in results),
               'p50_ms': statistics.median(times) if times else None, 'p95_ms': percentile(times, .95),
               'top1_changes': sum(r['top1_changed'] for r in results),
               'shortened_inputs': sum(i['shortened'] for r in results for i in r['inputs']),
               'selectors': dict(Counter(i['selector'] for r in results for i in r['inputs'])),
               'retrieval_manifest_sha256': digest(metadata),
               'tokenizer_sha256': hashlib.sha256(args.tokenizer.read_bytes()).hexdigest(),
               'limitations': ['Ranking changes and scores do not establish relevance improvement.',
                              'No paid or human relevance judgments were performed.',
                              'Indexed passage selection uses lexical overlap; reader/OCR fallback uses its prefix.',
                              'Scoring excerpts are explicitly bounded; full readers remain in the retrieval artifact.',
                              'Latency includes concurrent ingestion and live embedding load.']}
    save(args.out / 'summary.json', summary)
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
