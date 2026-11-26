"""Interpret Luna's request and token quota headers without storing response data."""

from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import re


DIMENSIONS = ('requests', 'tokens', 'project-tokens')
RESET_PART = re.compile(r'([0-9]{1,9}(?:\.[0-9]{1,3})?)(ms|s|m|h|d)')
SECONDS = {'ms': .001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}
MAX_DELAY = 366 * 86400
HEADROOM_PERCENT = 20
MINUTE_REQUEST_TARGET = 8_000
MINUTE_TOKEN_TARGET = 8_000_000


def count(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,12}', value.strip()):
        return int(value)
    return None


def reset_seconds(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    value = value.strip()
    offset = 0
    delay = 0
    for part in RESET_PART.finditer(value):
        if part.start() != offset:
            return None
        amount, unit = part.groups()
        delay += float(amount) * SECONDS[unit]
        offset = part.end()
    if offset and offset == len(value) and delay <= MAX_DELAY:
        return math.ceil(delay)
    return None


def retry_seconds(value, now):
    seconds = count(value)
    if seconds is not None:
        return seconds if seconds <= MAX_DELAY else None
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        seconds = math.ceil((stamp - now).total_seconds())
    except (AttributeError, OverflowError, TypeError, ValueError):
        return None
    return max(0, seconds) if seconds <= MAX_DELAY else None


def capacity(response):
    values = {}
    for dimension in DIMENSIONS:
        limit = count(response.getheader('x-ratelimit-limit-' + dimension))
        left = count(response.getheader('x-ratelimit-remaining-' + dimension))
        if limit and left is not None and left <= limit:
            reset = reset_seconds(response.getheader('x-ratelimit-reset-' + dimension))
            values[dimension] = {'limit': limit, 'remaining': left, 'reset_seconds': reset}
    return values


def cooldown(response, now, capacity, *, quota=False):
    """Return a minimum cooldown when remaining headroom is at most twenty percent."""
    if response.status not in (200, 429, 503):
        return 0
    delay = 0
    for value in capacity.values():
        if value['remaining'] * 100 <= value['limit'] * HEADROOM_PERCENT:
            reset = value['reset_seconds']
            delay = max(delay, 5 if reset is None else max(1, reset))
    if response.status == 200:
        return delay
    retry = retry_seconds(response.getheader('Retry-After'), now)
    minimum = 1800 if quota else 0
    if retry is not None:
        return max(1, retry, delay, minimum)
    return max(delay, minimum, 60 if response.status == 429 else 15)
