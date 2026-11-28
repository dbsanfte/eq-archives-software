"""Validated, revisioned credentials for the curation inference lanes."""

from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit

from common import CrawlError, digest
from luna_limits import MINUTE_REQUEST_TARGET, MINUTE_TOKEN_TARGET

LUNA_MODEL = 'gpt-6-luna'
LUNA_URL = 'https://api.openai.com/v1'
CHAT_APIS = ('chat', 'chat_completion_tokens')
PROFILE_FILE = '/run/secrets/ai_provider.json'
SECRET_NAME = 'eqarchives-curation-provider'
FIELDS = frozenset(('endpoint', 'model', 'api', 'format', 'context_tokens', 'output_tokens', 'input_price', 'output_price',
                    'requests_per_minute', 'tokens_per_minute'))


@dataclass(frozen=True)
class Provider:
    endpoint: str = LUNA_URL
    model: str = LUNA_MODEL
    api: str = 'responses'
    format: str = 'schema'
    context_tokens: int = 1_050_000
    output_tokens: int = 32_768
    input_price: float = .10
    output_price: float = .50
    requests_per_minute: int = MINUTE_REQUEST_TARGET
    tokens_per_minute: int = MINUTE_TOKEN_TARGET
    revision: int = 0
    api_key: str = field(default='', repr=False)

    @property
    def luna(self):
        return (self.endpoint, self.model, self.api, self.format) == (LUNA_URL, LUNA_MODEL, 'responses', 'schema')

    @property
    def cache_identity(self):
        if self.luna:
            return ''  # Retain the byte-identical Luna cache namespace.
        return digest((self.endpoint, self.model, self.api, self.format))

    def public(self):
        return {**{name: getattr(self, name) for name in FIELDS}, 'revision': self.revision,
                'key_configured': bool(self.api_key), 'builtin_pricing': self.luna}

    @property
    def uses_bootstrap_key(self):
        return not self.api_key and address(self.endpoint)[2] == 'https://api.openai.com'

    @property
    def free(self):
        return not self.luna and self.input_price == self.output_price == 0


DEFAULT_PROVIDER = Provider()


def address(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if (not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.scheme not in ('http', 'https') or not 1 <= port <= 65535):
            raise ValueError()
        if ':' in host:
            host = '[' + ipaddress.IPv6Address(host).compressed + ']'
        elif not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host):
            raise ValueError()
        origin = f'{parsed.scheme}://{host}'
        if port != (443 if parsed.scheme == 'https' else 80):
            origin += ':' + str(port)
        return parsed, port, origin
    except (TypeError, ValueError):
        raise CrawlError('Enter an HTTP or HTTPS API base URL without credentials, a query, or a fragment') from None


def trusted_private(origin):
    return origin in os.environ.get('CURATION_AI_PRIVATE_ORIGINS', '').split()


def normalize_endpoint(value):
    if (not isinstance(value, str) or len(value) > 512 or not value.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in value) or '\\' in value):
        raise CrawlError('Enter a valid API base URL')
    parsed, _, origin = address(value)
    if parsed.scheme != 'https' and not trusted_private(origin):
        raise CrawlError('Remote API endpoints require HTTPS')
    path = parsed.path.rstrip('/')
    if (path and not re.fullmatch(r'(?:/[A-Za-z0-9._~-]+)+', path)) or any(part in ('.', '..') for part in path.split('/')):
        raise CrawlError('Enter a base URL with a plain API path')
    if path.endswith(('/responses', '/chat/completions')):
        raise CrawlError('Enter the base URL before /responses or /chat/completions')
    return origin + path


def provider_from_payload(previous, data):
    if not isinstance(data, dict) or type(data.get('revision')) is not int or data['revision'] != previous.revision:
        raise CrawlError('Provider settings changed. Reload them before saving')
    if set(data) == {'revision', 'reset'} and data['reset'] is True:
        return replace(DEFAULT_PROVIDER, revision=previous.revision + 1)
    if set(data) != FIELDS | {'revision', 'api_key'}:
        raise CrawlError('Provider settings are incomplete')
    endpoint = normalize_endpoint(data['endpoint'])
    model, api, response_format = (data[name] for name in ('model', 'api', 'format'))
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,191}', model):
        raise CrawlError('Enter a valid provider model name')
    if api not in ('responses', *CHAT_APIS, 'gemini') or response_format not in ('schema', 'json', 'prompt'):
        raise CrawlError('Choose an API and an output format')
    context, output = data['context_tokens'], data['output_tokens']
    if (type(context) is not int or not 8192 <= context <= 2_000_000
            or type(output) is not int or (output != 0 and not 2048 <= output <= 65536) or output >= context):
        raise CrawlError('Use a context limit of 8,192–2,000,000 and an output limit of 2,048–65,536 below it, or zero for a self-hosted provider')
    prices = (data['input_price'], data['output_price'])
    if any(type(price) not in (int, float) or not 0 <= price <= 10_000 or not math.isfinite(price) for price in prices):
        raise CrawlError('Enter nonnegative input and output USD prices per million tokens')
    origin = address(endpoint)[2]
    if output == 0 and (api not in CHAT_APIS or not trusted_private(origin) or any(prices)):
        raise CrawlError('Zero output limit requires an allowlisted private Chat Completions provider with zero token prices')
    rpm, tpm = data['requests_per_minute'], data['tokens_per_minute']
    if (type(rpm) is not int or not 1 <= rpm <= MINUTE_REQUEST_TARGET
            or type(tpm) is not int or not 1000 <= tpm <= MINUTE_TOKEN_TARGET):
        raise CrawlError('Enter request and token rates within the curation pacing limits')
    key = data['api_key']
    if not isinstance(key, str) or len(key) > 2048 or any(not 33 <= ord(char) <= 126 for char in key):
        raise CrawlError('Enter a single API key without spaces or control characters')
    candidate = Provider(endpoint=endpoint, model=model, api=api, format=response_format, context_tokens=context,
                         output_tokens=output, input_price=prices[0], output_price=prices[1], requests_per_minute=rpm,
                         tokens_per_minute=tpm, revision=previous.revision + 1, api_key=key)
    if not key and origin == address(previous.endpoint)[2]:
        candidate = replace(candidate, api_key=previous.api_key)
    if not candidate.api_key and not candidate.uses_bootstrap_key and not trusted_private(origin):
        raise CrawlError('Enter an API key for this provider')
    # Resolve before saving and re-check every new socket. TLS uses the URL hostname.
    resolve(endpoint)
    return candidate


def encode_profile(provider):
    return json.dumps(asdict(provider), separators=(',', ':'))


def decode_profile(text):
    try:
        record = json.loads(text)
        if (not isinstance(record, dict) or set(record) != FIELDS | {'revision', 'api_key'}
                or type(record['revision']) is not int or record['revision'] < 1):
            raise ValueError()
        return Provider(**record)
    except (TypeError, ValueError):
        raise CrawlError('The saved AI provider profile is invalid') from None


@lru_cache(maxsize=4)
def _read(path, stat):
    with path.open() as stream:
        return decode_profile(stream.read(8193))


def current_provider(path=PROFILE_FILE):
    path = Path(path)
    try:
        stat = path.stat()
        if stat.st_size > 8192:
            raise CrawlError('The saved AI provider profile is too large')
        return _read(path, (stat.st_dev, stat.st_ino, stat.st_mtime_ns))
    except FileNotFoundError:
        return DEFAULT_PROVIDER
    except OSError:
        raise CrawlError('The saved AI provider profile is unavailable') from None


def resolve(endpoint):
    parsed, port, origin = address(endpoint)
    try:
        addresses = tuple(dict.fromkeys(record[4][0] for record in socket.getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM)))
        if not addresses:
            raise OSError()
    except OSError:
        raise CrawlError('The API provider address could not be resolved') from None
    if not trusted_private(origin):
        for value in addresses:
            ip = ipaddress.ip_address(value)
            ip = getattr(ip, 'ipv4_mapped', None) or ip
            if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_link_local or ip.is_loopback:
                raise CrawlError('The API endpoint resolves to a private or reserved address')
    return addresses, port


def connect_to_provider(endpoint, timeout, source_address=None):
    addresses, port = resolve(endpoint)
    for value in addresses:
        try:
            # A literal address removes the DNS-resolution gap before connecting.
            return socket.create_connection((value, port), timeout, source_address)
        except OSError:
            continue
    raise OSError('Provider connection failed')
