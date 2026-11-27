import base64
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
import socket
from unittest.mock import Mock

import pytest

import ai_provider
import grading
import provider_settings
from common import CrawlError
from daily_budget import DailyBudget, LunaRatePause
from indexer import capture_enrichment
from server import create_app
from test_enrichment import RESULT, SOURCE
from test_server import call


def form(provider=ai_provider.DEFAULT_PROVIDER, **changes):
    return {**{key: getattr(provider, key) for key in ai_provider.FIELDS},
            'revision': provider.revision, 'api_key': '', **changes}


def dns(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (value, 443)) for value in addresses]


def test_secret_round_trip_is_write_only_and_binds_a_key_to_its_origin(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: dns('8.8.8.8'))
    kube = Mock(namespace='eqarchives-es')
    saved = {'metadata': {'resourceVersion': '10'}, 'data': {}}

    def request(method, path, body=None):
        assert path == '/api/v1/namespaces/eqarchives-es/secrets/eqarchives-curation-provider'
        if method == 'PUT':
            assert body['metadata']['resourceVersion'] == saved['metadata']['resourceVersion']
            saved.update(metadata={'resourceVersion': '11'}, data=body['data'])
        return saved

    kube.request.side_effect = request
    config = form(endpoint='https://generativelanguage.googleapis.com/v1beta/openai/',
                  model='gemma-3-27b-it', api='chat', format='prompt', api_key='fixture-provider-key',
                  context_tokens=131072, output_tokens=8192, input_price=0, output_price=0)
    public = provider_settings.configure(kube, config)
    assert public['key_configured'] and public['revision'] == 1
    assert public['endpoint'].endswith('/openai')
    assert 'fixture-provider-key' not in json.dumps(public)
    profile = provider_settings.read(kube)[2]
    assert profile.api_key == 'fixture-provider-key' and 'fixture-provider-key' not in repr(profile)
    assert base64.b64decode(saved['data']['ai_provider.json']).decode() == ai_provider.encode_profile(profile)
    with pytest.raises(CrawlError, match='Reload'):
        provider_settings.configure(kube, config)
    with pytest.raises(CrawlError, match='Enter an API key'):
        provider_settings.configure(kube, form(profile, endpoint='https://other.example/v1'))
    rotated = provider_settings.configure(kube, form(profile, api_key='fixture-rotated'))
    assert rotated['revision'] == 2
    assert provider_settings.read(kube)[2].cache_identity == profile.cache_identity
    reset = provider_settings.configure(kube, {'reset': True, 'revision': 2})
    assert reset['model'] == grading.MODEL and not reset['key_configured']


def test_portal_rejects_external_or_cross_origin_key_writes(tmp_path, monkeypatch):
    monkeypatch.setenv('KUBERNETES_SERVICE_HOST', 'fixture')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: dns('8.8.8.8'))
    kube = Mock(namespace='eqarchives-es')
    stored = {'metadata': {'resourceVersion': '1'}, 'data': {}}

    def request(method, path, body=None):
        if method == 'PUT':
            stored['data'] = body['data']
        return stored

    kube.request.side_effect = request
    monkeypatch.setattr('jobs.Kubernetes', lambda: kube)
    app = create_app(tmp_path/'state', start_worker=True)
    path = '/api/ai-provider'
    payload = form(endpoint='https://provider.example/v1', model='demo-model', api='chat', api_key='one-time-key')
    assert call(app, 'POST', path, payload, peer='203.0.113.1').status_code == 403
    assert call(app, 'POST', path, payload, headers={'Origin': 'http://evil.example'}).status_code == 403
    assert call(app, 'GET', path, peer='203.0.113.1').status_code == 403
    kube.request.assert_not_called()
    result = call(app, 'POST', path, payload)
    assert result.status_code == 200 and result.headers['Cache-Control'] == 'no-store'
    assert result.json()['revision'] == 1 and result.json()['model'] == 'demo-model'
    assert result.json()['applied_revision'] == 0 and result.json()['key_configured']
    assert 'one-time-key' not in result.text and 'one-time-key' not in call(app, 'GET', path).text


@pytest.mark.parametrize('endpoint', ['file:///etc/hosts', 'https://user:pass@provider.example/v1',
    'http://public.example/v1', 'https://provider.example/v1?key=secret', 'https://provider.example/v1#token',
    'https://provider.example/v1/../admin', 'https://provider.example/v1/responses',
    'https://provider.example/v1/chat/completions', 'https://provider.example/v1/%2f', 'https://provider.example:65536/v1'])
def test_endpoint_rejects_unsafe_or_ambiguous_addresses(endpoint):
    with pytest.raises(CrawlError):
        ai_provider.normalize_endpoint(endpoint)


def test_connection_pins_resolved_addresses_and_rejects_private_answers(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', Mock(return_value=dns('8.8.8.8', '10.0.0.1')))
    connect = Mock()
    monkeypatch.setattr(socket, 'create_connection', connect)
    with pytest.raises(CrawlError, match='private'):
        ai_provider.connect_to_provider('https://provider.example/v1', 5)
    connect.assert_not_called()
    socket.getaddrinfo.return_value = dns('8.8.8.8')
    ai_provider.connect_to_provider('https://provider.example/v1', 4)
    connect.assert_called_once_with(('8.8.8.8', 443), 4, None)
    monkeypatch.setenv('CURATION_AI_PRIVATE_ORIGINS', 'http://192.168.1.15:8000')
    socket.getaddrinfo.return_value = dns('192.168.1.15')
    assert ai_provider.normalize_endpoint('http://192.168.1.15:8000/v1/') == 'http://192.168.1.15:8000/v1'
    ai_provider.connect_to_provider('http://192.168.1.15:8000/v1', 3)
    connect.assert_called_with(('192.168.1.15', 8000), 3, None)


@pytest.mark.parametrize(('api', 'mode', 'suffix'), [('chat', 'prompt', '/v1/chat/completions'),
    ('chat_completion_tokens', 'schema', '/v1/chat/completions'), ('chat', 'json', '/v1/chat/completions'),
    ('responses', 'schema', '/v1/responses'), ('gemini', 'prompt', '/v1/models/models-example:generateContent')])
def test_alternate_wire_protocols_keep_source_and_validate_provider_usage(tmp_path, api, mode, suffix):
    bootstrap = tmp_path/'key'
    bootstrap.write_text('bootstrap-key')
    configured = replace(ai_provider.DEFAULT_PROVIDER, api=api, format=mode, model='models-example',
                         endpoint='https://provider.example/v1', revision=1, api_key='local-fixture',
                         input_price=.15, output_price=.60)
    with closing(grading.Luna(bootstrap)) as client:
        client.use_provider(configured)
        received = ({'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                         {'text': 'hidden thought', 'thought': True}, {'text': '{"ok":true}'}]}}],
                     'usageMetadata': {'promptTokenCount': 1000, 'candidatesTokenCount': 200, 'thoughtsTokenCount': 100}}
                    if api == 'gemini' else {'choices': [{'finish_reason': 'stop', 'message': {'content': '```json\n{"ok":true}\n```'}}],
                                            'usage': {'prompt_tokens': 1000, 'completion_tokens': 300}})
        native = {'status': 'completed', 'usage': {'input_tokens': 1000, 'output_tokens': 300},
                  'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}]}
        client._post = Mock(return_value=native if api == 'responses' else received)
        payload = {'model': configured.model, 'input': 'Original source', 'instructions': 'Extract evidence', 'max_output_tokens': 8192,
                   'store': False, 'reasoning': {'effort': 'low'}, 'text': {'format': {'type': 'json_schema', 'name': 'archive',
                        'schema': {'type': 'object'}, 'strict': True}}}
        result = client.request(payload)
        path, wire, _ = client._post.call_args.args
        assert path == suffix and 'Original source' in json.dumps(wire) and 'reasoning' not in wire
        if api == 'chat_completion_tokens':
            assert wire['max_completion_tokens'] == 8192 and wire['response_format']['type'] == 'json_schema'
        if mode == 'prompt':
            assert 'response_format' not in wire and 'Return one complete JSON object' in json.dumps(wire)
        assert result['output'][0]['content'][0]['text'] == '{"ok":true}'
        assert grading.provider_usage_cost(configured, result['usage']) == .00033


def test_same_running_enricher_switches_request_and_tariff_between_records(tmp_path, monkeypatch):
    profile_path = tmp_path/'ai-provider.json'
    key_file = tmp_path/'key'
    key_file.write_text('bootstrap-key')
    reads = lambda: ai_provider.current_provider(profile_path)
    monkeypatch.setattr(capture_enrichment, 'current_provider', reads)
    initial = replace(ai_provider.DEFAULT_PROVIDER, revision=1, api='chat', format='prompt',
                      model='first-model', endpoint='https://first.example/v1', api_key='first-fixture',
                      input_price=1, output_price=2)
    alternate = replace(initial, revision=2, endpoint='https://second.example/v1', api_key='second-fixture',
                        model='second-model', input_price=2, output_price=4)

    def project(provider):
        temporary = tmp_path/'new.json'
        temporary.write_text(ai_provider.encode_profile(provider))
        temporary.replace(profile_path)

    project(initial)
    seen = []

    def post(client, path, payload, limit):
        seen.append((client.key, client.connection.host, payload['model']))
        if client.provider == initial:
            project(alternate)  # Update arrives after the first record's reservation.
        return {'model': payload['model'], 'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(RESULT)}}],
                'usage': {'prompt_tokens': 1000, 'completion_tokens': 200}}

    monkeypatch.setattr(grading.Luna, '_post', post)
    capture = {'archive_path': 'websites/site.example/20000101000000/raid.html', 'sha256': 'a'*64,
               'url': 'http://site.example/raid.html'}
    with closing(capture_enrichment.Enricher(tmp_path/'attempts', key_file)) as client:
        first = client.enrich(capture, SOURCE)
        second = client.enrich(capture, SOURCE)
        assert first['llm_enrichment_signature'] != second['llm_enrichment_signature']
        assert client.enrich(capture, SOURCE) == second
        assert seen == [('first-fixture','first.example','first-model'), ('second-fixture','second.example','second-model')]
        actual = [row[0] for row in client.store.db.execute('SELECT actual FROM attempts ORDER BY id')]
        assert actual == [.0014, .0028]


def test_rotation_discards_stale_provider_cooldowns_without_releasing_reservations(tmp_path):
    ledger = DailyBudget(tmp_path/'ledger')
    when = datetime.now(timezone.utc)
    receipt = ledger.reserve(.10)
    ledger.observe_luna_capacity(when, 600, {})
    assert ledger.snapshot()['luna_retry_at']
    ledger.activate_provider(replace(ai_provider.DEFAULT_PROVIDER, revision=1))
    ledger.observe_luna_capacity(when, 300, {}, provider_revision=0)
    fresh = ledger.snapshot()
    assert not fresh['luna_retry_at'] and fresh['unresolved_usd'] == .10
    assert ledger.settle(receipt, .06) == .06
    ledger.observe_luna_capacity(when, 120, {}, provider_revision=1)
    assert ledger.snapshot()['luna_retry_at']


def test_smaller_provider_shares_a_minute_limit_across_worker_instances(tmp_path):
    ledger = DailyBudget(tmp_path/'ledger')
    peer = DailyBudget(tmp_path/'ledger')
    ledger.activate_provider(replace(ai_provider.DEFAULT_PROVIDER, revision=1, requests_per_minute=2))
    first = ledger.reserve(0.10)
    second = peer.reserve(0.10)
    with pytest.raises(LunaRatePause):
        peer.reserve(0.10)
    ledger.activate_provider(replace(ai_provider.DEFAULT_PROVIDER, revision=2))
    assert peer.reserve(0.10)
    assert ledger.settle(first, .06) == .06 and peer.settle(second, .05) == .05
