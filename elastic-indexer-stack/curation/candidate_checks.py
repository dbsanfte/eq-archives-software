"""Explicit one-site evidence recovery, with durable budgets and guarded results."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from acquisition import sample
from common import CrawlError, Store, candidate_exclusion, digest, now
from coverage_check import refresh, require_new
from crawler import parser
from ezboard import address as ezboard_address, shard as ezboard_shard
from grading import criteria, grade, sources
from portal import Stage, decorate
from review import record
from state import active_operation, connect, enqueue, unpack

ELIGIBLE = {'approval_pending', 'discovered', 'sampled', 'sample_error', 'unavailable',
            'identity_unresolved', 'grade_error', 'coverage_unverified'}


def attach_checks(store, rows):
    latest = {}
    for item in store.db.execute("SELECT * FROM operations WHERE kind='candidate_check' ORDER BY created,rowid"):
        operation = unpack(item)
        latest[operation['payload']['id']] = {
            'id': operation['id'], 'state': operation['state'], 'error': operation['error'],
            'phase': (operation['result'] or {}).get('phase'), 'max_usd': operation['payload']['max_usd'],
            'grading_criteria': operation['payload'].get('grading_criteria', '')}
    for row in rows:
        row['candidate_check'] = latest.get(row['id'])
    return rows


def current(store, payload):
    raw = store.db.execute('SELECT * FROM candidates WHERE id=?', (payload['id'],)).fetchone()
    if not raw:
        raise CrawlError('Unknown candidate')
    row = decorate(store, [record(store, raw)])[0]
    if reason := candidate_exclusion(row['url']):
        raise CrawlError(reason)
    if ezboard_shard(urlsplit(row['url']).hostname or '') and not ezboard_address(row['url']):
        raise CrawlError('Ezboard profiles, forms and unsupported server URLs cannot be graded as sites. Submit a top-level board URL.')
    if row['stage'] != Stage.CANDIDATES or row['state'] not in ELIGIBLE or row['manifest_sha256'] != payload['manifest_sha256']:
        raise CrawlError('Candidate changed. Refresh it before requesting evidence and grading.')
    return raw, row


def source_identity(row):
    return digest({'url': row['url'], 'captures': row['captures']})


def require_finished(store, row):
    if store.db.execute("SELECT 1 FROM operations WHERE kind='candidate_check' AND state IN ('queued','running') AND json_extract(payload,'$.id')=?", (row['id'],)).fetchone():
        raise CrawlError('Wait for this site’s evidence and grading check before approving capture.')


def start(store, payload):
    store.db.execute('BEGIN IMMEDIATE')
    _, row = current(store, payload)
    previous = store.db.execute("SELECT * FROM operations WHERE kind='candidate_check' AND json_extract(payload,'$.id')=? ORDER BY created DESC,rowid DESC LIMIT 1", (row['id'],)).fetchone()
    previous = unpack(previous) if previous else None
    focus = criteria(payload.get('grading_criteria', previous['payload'].get('grading_criteria', '') if previous else (row['rating'] or {}).get('grading_criteria', '')))
    payload = {**payload, 'grading_criteria': focus}
    if previous and previous['state'] in ('queued', 'running'):
        if focus != previous['payload'].get('grading_criteria', ''):
            raise CrawlError('This check is already running with saved grading criteria. Wait for it to finish before changing them.')
        return {'operation': previous['id'], 'max_usd': previous['payload']['max_usd'], 'existing': True}
    if row['rating'] and row['captures'] and focus == row['rating'].get('grading_criteria', ''):
        raise CrawlError('This candidate already has a source grade for these criteria. Review its capture scope or change the criteria.')
    if previous:
        if (previous['payload']['manifest_sha256'] != row['manifest_sha256']
                and previous['payload'].get('source_sha256') != source_identity(row)):
            raise CrawlError('Evidence changed since this check. Its saved results and budget are retained.')
        if active_operation(store):
            raise CrawlError('Another capture, discovery or evidence check is queued or running. Retry when it finishes.')
        # A changed capture scope can reuse the same samples, cached grade and
        # budget. Bind the next merge to the newly reviewed scope, never an old one.
        resumed = {**previous['payload'], 'manifest_sha256': row['manifest_sha256'], 'grading_criteria': focus}
        store.db.execute('INSERT INTO events(candidate,action,detail,created) VALUES (?,?,?,?)',
                         (row['id'], 'grading_requested', json.dumps({'operation': previous['id'], 'grading_criteria': focus}), now()))
        store.db.execute("UPDATE operations SET state='queued',payload=?,error=NULL,updated=? WHERE id=?",
                         (json.dumps(resumed), now(), previous['id']))
        store.db.commit()
        return {'operation': previous['id'], 'max_usd': previous['payload']['max_usd'], 'existing': True}
    operation = enqueue(store, 'candidate_check', {**payload, 'source_sha256': source_identity(row)}, commit=False)
    store.db.commit()
    return {'operation': operation, 'max_usd': payload['max_usd'], 'existing': False}


def run(root, operation):
    payload = operation['payload']
    def progress(phase):
        with connect(root) as main:
            main.db.execute('UPDATE operations SET result=?,updated=? WHERE id=?',
                            (json.dumps({'phase': phase}), now(), operation['id']))
            main.db.commit()
    progress('coverage')
    refresh(root, candidate_id=payload['id'], force=True)
    with connect(root) as main:
        raw, row = current(main, payload)
        if row['rating'] and row['captures'] and row['rating'].get('grading_criteria', '') == payload.get('grading_criteria', ''):
            return {'candidate_id': row['id'], 'phase': 'complete'}
        if not os.environ.get('ARCHIVE_REPO'):
            raise CrawlError('Archive metadata is not configured; evidence recovery cannot continue.')
        require_new(main, row['url'])
        original = dict(raw)
        if row['captures']:
            sources(main, row, 120000)  # Refuse changed/missing sources before any paid request.
    directory = Path(root) / 'runs' / operation['id']
    run_store = Store(directory)
    try:
        if not (directory / 'initialized').exists():
            retained = {}
            captures = []
            for index, capture in enumerate(row['captures']):
                path = f'reused/{index}.html'
                destination = directory / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    os.link(Path(root) / capture['path'], destination)
                retained[path] = capture['path']
                captures.append({**capture, 'path': path})
            original.update(captures=json.dumps(captures), decision=None)
            keys = list(original)
            run_store.db.execute(f"INSERT OR REPLACE INTO candidates({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", list(original.values()))
            run_store.set('retained_sources', retained)
            (directory / 'initialized').touch(mode=0o600)
        # A saved scope edit affects redirect eligibility on retry. Retain the
        # same operation, sources, cached responses and cumulative budgets.
        run_store.db.execute('UPDATE candidates SET scope=?,coverage=? WHERE id=?',
                             (row['scope'], json.dumps(row['coverage']), row['id']))
        run_store.db.commit()
        options = parser()
        candidate = run_store.candidates()[0]
        if not json.loads(candidate['captures']):
            progress('sampling')
            args = options.parse_args(['--work-dir', str(directory), 'sample', '--max-candidates', '1',
                                       '--max-seconds', '1800', '--retry-unresolved'])
            sample(args, run_store)
        if json.loads(run_store.candidates()[0]['captures']):
            progress('grading')
            args = options.parse_args(['--work-dir', str(directory), 'grade', '--max-candidates', '1',
                                       '--api-key-file', '/run/secrets/luna_api_key', '--max-usd', str(payload['max_usd'])])
            args.grading_criteria = payload.get('grading_criteria', '')
            grade(args, run_store)
        checked = record(run_store, run_store.candidates()[0])
        retained = run_store.get('retained_sources', {})
        captures = [{**capture, 'path': retained.get(capture['path'], f"runs/{operation['id']}/" + capture['path'])}
                    for capture in checked['captures']]
        # A concurrent dismissal, approval or new manifest wins. Keep recovered
        # artifacts in this run, without overwriting a later human decision.
        with connect(root) as main:
            main.db.execute('BEGIN IMMEDIATE')
            current(main, payload)
            main.db.execute('UPDATE candidates SET captures=?,rating=?,state=?,error=? WHERE id=?',
                            (json.dumps(captures), json.dumps(checked['rating']) if checked['rating'] else None,
                             checked['state'], checked['error'], row['id']))
            updated = record(main, main.db.execute('SELECT * FROM candidates WHERE id=?', (row['id'],)).fetchone())
            updated_payload = {**payload, 'manifest_sha256': updated['manifest_sha256'], 'source_sha256': source_identity(updated)}
            main.db.execute('UPDATE operations SET payload=? WHERE id=?', (json.dumps(updated_payload), operation['id']))
            main.db.commit()
        if not checked['rating']:
            raise CrawlError(checked['error'] or 'No readable Wayback samples are available to grade. Retry or dismiss this site.')
        return {'candidate_id': row['id'], 'phase': 'complete', 'grade': checked['rating']['grade']}
    finally:
        run_store.close()
