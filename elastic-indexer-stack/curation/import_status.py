"""Small, manifest/Job-bound import diagnostics with no upstream response text."""

import json
from pathlib import Path
import re
from datetime import datetime

from common import CrawlError, now, save
from daily_budget import DailyBudgetPause, units

MESSAGES = {
    'schema': 'Luna enrichment failed schema/source validation: invalid metadata fields.',
    'source_evidence': 'Luna enrichment failed source validation: date evidence is not a verbatim source excerpt.',
    'incomplete': 'Luna enrichment is incomplete; indexing remains pending.',
    'refused': 'Luna declined enrichment; indexing remains pending.',
    'budget': 'Enrichment dollar budget reached; paid results are retained.',
    'context': 'Complete source exceeds the enrichment context bound; no text truncated or document created.',
    'request': 'Luna request failed or its outcome is uncertain; spend reservations are retained.',
    'usage': 'Invalid enrichment usage; spend reservation retained.',
    'correction_limit': 'AI enrichment stopped after two correction attempts. Operator attention is required; retrying will reuse saved responses.',
    'indexing': 'Capture import failed; verified sources and saved AI results are retained.',
}


class EnrichmentError(CrawlError):
    def __init__(self, code, cause=None):
        self.code, self.cause = code, cause
        super().__init__(describe(code, cause))


def describe(code, cause=None):
    message = MESSAGES.get(code, MESSAGES['indexing'])
    if cause in MESSAGES and cause != code:
        message += ' ' + MESSAGES[cause]
    return message


def status_path(directory, job_name):
    if not re.fullmatch(r'eqarchives-captures-[a-f0-9]{32}(?:-r[1-9][0-9]{0,5})?', job_name):
        raise ValueError('Invalid import Job name')
    return Path(directory) / ('status-' + job_name + '.json')


def write_status(directory, job_name, manifest_sha256, state, error=None):
    if not job_name:  # Standalone operator imports still report through stdout.
        return
    record = {'job_name': job_name, 'manifest_sha256': manifest_sha256,
              'state': state, 'updated_at': now()}
    if error is not None:
        record.update(code=error.code if isinstance(error, EnrichmentError) else 'indexing',
                      cause=error.cause if isinstance(error, EnrichmentError) else None)
        if isinstance(error, DailyBudgetPause):
            record.update(required_usd=error.amount, retry_at=error.retry_at)
    save(status_path(directory, job_name), record)


def read_status(root, batch, job_name):
    try:
        path = status_path(Path(root) / 'enrichment' / batch['id'], job_name)
        if path.stat().st_size > 4096:
            return None
        record = json.loads(path.read_text())
        if record.get('job_name') != job_name or record.get('manifest_sha256') != batch['manifest_sha256']:
            return None
        return record
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def read_error(root, batch, job_name):
    record = read_status(root, batch, job_name) or {}
    try:
        if record.get('state') == 'failed' and record.get('code') in MESSAGES:
            return describe(record['code'], record.get('cause'))
    except TypeError:
        return None


def read_budget_pause(root, batch, job_name):
    record = read_status(root, batch, job_name) or {}
    if record.get('state') != 'waiting_budget':
        return None
    try:
        if not 0 < units(record['required_usd']) or datetime.fromisoformat(record['retry_at']).utcoffset() is None:
            return None
    except (CrawlError, KeyError, TypeError, ValueError):
        return None
    return {key: record[key] for key in ('required_usd', 'retry_at')}
