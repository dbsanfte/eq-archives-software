"""An independently checkpointed, budgeted pass within rolling maintenance."""

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
import re
import sqlite3
import time
from urllib.parse import urlsplit

from common import CrawlError, digest
from daily_budget import AutomaticStopped, DailyBudget, DailyBudgetPause, EnrichmentPriorityPause
from grading import Luna
from import_status import EnrichmentError
from indexer import gemma, passages, search_enrichment
from indexer.archive_handler import ArchiveHandler
from indexer.capture_enrichment import Enricher, summary_requests
from policy_preparation import newer, prepare
from policy_sources import ServiceError, SourceError, relative


ORIGINAL_POLICY = 'eq-passages-480-v13'  # First release of eq-topics-v1.
MAX_ATTEMPTED = 8
SLICE_SECONDS = 75
COUNTERS = ('updated', 'reused', 'failed', 'skipped')
COLLECTIONS = ('websites', 'mailing-lists', 'newsgroups')
APP = 'eqarchives-rolling-enrichment'


class SliceDone(Exception):
    pass


def current_enrichment():
    versions = [{'term': {'llm_enrichment_passage_version': passages.VERSION}}]
    if passages.VERSION == ORIGINAL_POLICY:
        versions.append({'bool': {'must_not': [{'exists': {'field': 'llm_enrichment_passage_version'}}]}})
    return {'bool': {'filter': [{'term': {'llm_search_enrichment_version': search_enrichment.VERSION}}],
                     'should': versions, 'minimum_should_match': 1}}


def eligible(source):
    version = source.get('llm_search_enrichment_version')
    checkpoint = source.get('llm_enrichment_passage_version')
    return (source.get('file_type') == 'text' and source.get('embedding_model') == gemma.MODEL
            and source.get('text_chunking_version') == passages.VERSION
            and (not version or version == search_enrichment.VERSION and
                 checkpoint != passages.VERSION and
                 (not newer(checkpoint) if checkpoint is not None else passages.VERSION != ORIGINAL_POLICY)))


def capture_identity(identifier):
    parts = relative(identifier).parts
    collection = parts[0]
    if collection == 'websites':
        if len(parts) < 4 or not re.fullmatch('[0-9]{14}', parts[2]):
            raise SourceError('source_identity')
        url = 'http://' + parts[1] + '/' + '/'.join(parts[3:])
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username is not None or parsed.netloc != parts[1]:
            raise SourceError('source_identity')
        host = parsed.hostname.lower().rstrip('.').removeprefix('www.')
        port = parsed.port
        return url, (collection, host, port if port not in (80, 443) else None)
    if len(parts) < 2:
        raise SourceError('source_identity')
    scope = parts[1] if collection == 'mailing-lists' else re.sub(r'-msg-[0-9]+\.txt$', '', parts[1])
    return ArchiveHandler()._convert_to_archive_url(identifier), (collection, scope)


@dataclass(frozen=True)
class EnrichmentInput:
    hit: dict
    capture: dict | None = None
    source: str = ''
    view: str = ''
    grounding: tuple = ()


def prepare_refresh(services, sources, identifier, url, check):
    hit = services.get(identifier)
    source = hit['_source']
    if not eligible(source):
        return None
    prepared = prepare(hit, sources, enrichment=True)
    if prepared.enrichment_source is None:
        return None
    view = prepared.plan.view
    parts = search_enrichment.evidence_parts(view)
    if not parts:
        return None
    check()
    revision = {key: hit[key] for key in ('_id', '_seq_no', '_primary_term')}
    if (source.get('llm_search_enrichment_version') == search_enrichment.VERSION
            and source.get('llm_enrichment_view_sha256') == digest(parts)):
        return EnrichmentInput(revision)
    body, sha256 = prepared.enrichment_source
    return EnrichmentInput(revision, {'url': url, 'archive_path': identifier, 'sha256': sha256}, body, view, parts)


def metadata_patch(metadata):
    if metadata.get('llm_search_enrichment_version') != search_enrichment.VERSION:
        raise EnrichmentError('context')
    # Publication estimates and their original provenance remain as indexed.
    return {field: metadata[field] for field in (
        'llm_summary', 'llm_tags', 'llm_content_flavour', 'llm_model_name', 'llm_enrichment_signature',
        'llm_content_role', 'llm_topics', 'llm_entities', 'llm_search_enrichment_version', 'llm_enrichment_view_sha256')}


def refresh(services, sources, identifier, url, enricher, check):
    prepared = prepare_refresh(services, sources, identifier, url, check)
    if prepared is None:
        return 'skipped'
    checkpoint = {'llm_enrichment_passage_version': passages.VERSION}
    if prepared.capture is None:
        services.update(prepared.hit, checkpoint)
        return 'reused'
    patch = metadata_patch(enricher.enrich(prepared.capture, prepared.source, prepared.view))
    check()
    requests = summary_requests(patch['llm_summary'])
    vectors = services.vectors(requests, check) if requests else []
    if len(vectors) != len(requests):
        raise EnrichmentError('indexing')
    try:
        vector = gemma.mean_vector(vectors)
    except (TypeError, ValueError):
        raise EnrichmentError('indexing') from None
    check()
    services.update(prepared.hit, {**patch, **checkpoint, 'llm_summary_vector': vector})
    return 'updated'


def run_enrichment(state, services, sources, owner, budget_root, check, *, seconds=SLICE_SECONDS,
                   enricher_factory=Enricher):
    """Use a small slice before the body pass, even when the body cursor is idle."""
    clock = time.monotonic
    deadline = clock() + seconds
    progress = state.enrichment_progress(search_enrichment.VERSION)
    if progress['next_at'] > time.time():
        return progress

    def allowed():
        check()
        if clock() >= deadline:
            raise SliceDone()

    def report(phase, *, next_at=0):
        return state.enrichment_progress(search_enrichment.VERSION, phase=phase, next_at=next_at)

    try:
        account = DailyBudget(budget_root)
        budget = account.snapshot(maintenance=True)
        if not budget['complete']:
            return report('waiting_configuration', next_at=time.time() + 300)
        if budget['remaining_usd'] <= 0:
            return report('waiting_budget', next_at=datetime.fromisoformat(budget['resets_at']).timestamp())
        if budget['enrichment_pending']:
            return report('waiting_imports', next_at=time.time() + 60)
        allowed()
        batch = services.select_enrichment(progress['cursor'])
        if not batch:
            return state.enrichment_progress(search_enrichment.VERSION, phase='sweep_waiting',
                                             next_at=time.time() + 3600, cursor='')
        capped = set()
        attempted = 0
        def client(key_file):
            return Luna(key_file, deadline=time.time() + max(0, deadline - clock()))

        with ExitStack() as resources, account.bind(purpose='maintenance', owner=owner):
            enrichers = {}
            for identifier in batch:
                allowed()
                reason = None
                try:
                    url, scope = capture_identity(identifier)
                    if scope in capped:
                        result = 'skipped'
                    else:
                        attempted += 1
                        if scope not in enrichers:
                            enricher = enricher_factory(state.root/'policy-enrichment'/digest(scope),
                                '/run/secrets/luna_api_key', client_factory=client, check=allowed)
                            resources.callback(enricher.close)
                            enrichers[scope] = enricher
                        try:
                            result = refresh(services, sources, identifier, url, enrichers[scope], allowed)
                        except SourceError as error:
                            if error.reason != 'concurrent_update':
                                raise
                            # Recheck the exact source once. Any paid response
                            # already cached for this input is reused.
                            result = refresh(services, sources, identifier, url, enrichers[scope], allowed)
                except EnrichmentError as error:
                    reason = 'enrichment_' + error.code
                    if error.code == 'budget':
                        capped.add(scope)
                        result = 'skipped'
                    else:
                        result = 'failed'
                except SourceError as error:
                    result, reason = 'failed', error.reason
                except ServiceError:
                    raise
                except (TypeError, KeyError, ValueError):
                    result, reason = 'failed', 'source_validation'
                state.enrichment_progress(search_enrichment.VERSION, cursor=identifier, result=result, reason=reason)
                if attempted >= MAX_ATTEMPTED:
                    break
        return report('bounded')
    except SliceDone:
        return report('bounded')
    except DailyBudgetPause as error:
        return report('waiting_budget', next_at=datetime.fromisoformat(error.retry_at).timestamp())
    except EnrichmentPriorityPause:
        return report('waiting_imports', next_at=time.time() + 60)
    except AutomaticStopped:
        return report('waiting_configuration', next_at=time.time() + 300)
    except (CrawlError, ValueError, OSError, sqlite3.Error):
        return report('retry_wait', next_at=time.time() + 300)
