from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
import json

import pytest

import daily_budget
import grading
from daily_budget import DailyBudget, LunaRatePause
from grading import Luna
from luna_limits import capacity, cooldown, reset_seconds


STAMP = datetime(2026, 11, 26, 14, 30, tzinfo=timezone.utc)


class Response:
    def __init__(self, status, headers=None, body=None):
        self.status = status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.body = json.dumps(body or {}).encode()

    def read(self, _):
        return self.body

    def getheader(self, name):
        return self.headers.get(name.lower())


class Connection:
    sock = None

    def __init__(self, response):
        self.response = response
        self.requests = 0

    def request(self, *_):
        self.requests += 1

    def getresponse(self):
        return self.response

    def close(self):
        pass


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        stamp = STAMP

        def __call__(self):
            return self.stamp

        def advance(self, seconds):
            self.stamp += timedelta(seconds=seconds)

    timer = Clock()
    monkeypatch.setattr(daily_budget, 'instant', timer)
    monkeypatch.setattr(grading, 'instant', timer)
    return timer


def client(directory, response):
    path = Path(directory) / 'key'
    path.write_text('fixture-key')
    luna = Luna(path)
    luna.connection = Connection(response)
    return luna


def test_low_project_quota_and_out_of_order_healthy_headers_share_hold(tmp_path, clock):
    near = Response(200, {
        'X-RateLimit-Limit-Tokens': '10000000', 'X-RateLimit-Remaining-Tokens': '9700000',
        'X-RateLimit-Limit-Project-Tokens': '2000000', 'X-RateLimit-Remaining-Project-Tokens': '381000',
        'X-RateLimit-Reset-Project-Tokens': '7.5s',
    }, {'usage': {'input_tokens': 12, 'output_tokens': 3}})
    first = DailyBudget(tmp_path / 'budget')
    live_attempt = first.reserve(.001)
    with first.bind(), closing(client(tmp_path, near)) as luna:
        assert luna.request({'model': grading.MODEL})['usage']['input_tokens'] == 12
    first.observe_luna_capacity(STAMP - timedelta(seconds=1), 0, {'tokens': {'limit': 10, 'remaining': 9}})
    first.settle(live_attempt, .0000027, usage={'input_tokens': 12, 'output_tokens': 3})
    second = DailyBudget(tmp_path / 'budget')
    assert second.snapshot()['luna_retry_at'] == (STAMP + timedelta(seconds=8)).isoformat()
    with pytest.raises(LunaRatePause):
        second.reserve(.001)
    assert second.snapshot()['luna_recent_requests'] == 1
    clock.advance(8)
    assert second.reserve(.001)


def test_retry_after_and_http_status_back_off_before_the_next_paid_request(tmp_path, clock):
    account = DailyBudget(tmp_path / 'budget')
    throttled = Response(429, {'Retry-After': format_datetime(STAMP + timedelta(seconds=21), usegmt=True)},
                         {'error': {'code': 'rate_limit_exceeded'}})
    with account.bind(), closing(client(tmp_path, throttled)) as luna:
        with pytest.raises(LunaRatePause) as error:
            luna.request({'model': grading.MODEL})
        assert error.value.status == 429 and luna.connection.requests == 1
        with pytest.raises(LunaRatePause) as waiting:
            luna.count_input_tokens({'model': grading.MODEL})
        assert waiting.value.retry_at == (STAMP + timedelta(seconds=21)).isoformat()
        assert waiting.value.status is None and luna.connection.requests == 1
    clock.advance(21)
    assert account.reserve(.001)


@pytest.mark.parametrize(('status', 'retry', 'reset', 'quota', 'delay'), [
    (200, None, '2m3.25s', False, 124),
    (429, '15', '18s', False, 18),
    (429, '15', 'bad', True, 1800),
    (429, 'bad', 'bad', False, 60),
    (503, '0', 'bad', False, 5),
    (403, '77', '20s', False, 0),
])
def test_provider_reset_and_throttle_variants(status, retry, reset, quota, delay):
    response = Response(status, {'x-ratelimit-limit-requests': '10000', 'x-ratelimit-remaining-requests': '2000',
                                 'x-ratelimit-reset-requests': reset, 'retry-after': retry})
    assert cooldown(response, STAMP, capacity(response), quota=quota) == delay


def test_missing_and_malformed_rate_headers_are_ignored():
    response = Response(200, {'x-ratelimit-limit-requests': '10000', 'x-ratelimit-remaining-requests': '-1',
                              'x-ratelimit-limit-tokens': '100', 'x-ratelimit-remaining-tokens': '101'})
    assert capacity(response) == {}
    assert cooldown(response, STAMP, {}) == 0
    assert reset_seconds('2m 1s') is None and reset_seconds('249ms') == 1


@pytest.mark.parametrize('dimension', ['requests', 'tokens'])
def test_local_minute_threshold_expires_even_without_provider_headers(tmp_path, clock, monkeypatch, dimension):
    monkeypatch.setattr(daily_budget, 'MINUTE_REQUEST_TARGET', 2 if dimension == 'requests' else 8000)
    monkeypatch.setattr(daily_budget, 'MINUTE_TOKEN_TARGET', 25 if dimension == 'tokens' else 8000000)
    account = DailyBudget(tmp_path / 'budget')
    for _ in range(2):
        receipt = account.reserve(.001)
        account.settle(receipt, .0000027, usage={'input_tokens': 12, 'output_tokens': 3})
        account.settle(receipt, .0000027, usage={'input_tokens': 12, 'output_tokens': 3})
    assert account.snapshot()['luna_recent_tokens'] == 30
    assert account.snapshot()['luna_accounted_tokens_today'] == 30
    clock.advance(59)
    with pytest.raises(LunaRatePause) as error:
        account.reserve(.001)
    assert error.value.retry_at == (STAMP + timedelta(seconds=60)).isoformat()
    clock.advance(1)
    assert account.snapshot()['luna_recent_requests'] == 0
    assert account.reserve(.001)


def test_daily_token_guard_includes_old_receipts_and_retains_paid_usage(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(daily_budget, 'DAILY_TOKEN_TARGET', 1000)
    account = DailyBudget(tmp_path / 'budget')
    old = account.reserve(.00004)
    account.settle(old, .00004)  # an older image recorded spend only
    new = account.reserve(.00004)
    account.settle(new, .00004, usage={'input_tokens': 175, 'output_tokens': 45})
    assert account.snapshot()['luna_accounted_tokens_today'] == 620
    with pytest.raises(LunaRatePause) as error:
        account.reserve(.00004)
    assert error.value.retry_at == '2026-11-27T00:00:00+00:00'
    assert account.snapshot()['luna_recent_requests'] == 2
    clock.advance(86400)
    assert account.reserve(.00004)
