"""Read and update the one operator-owned inference Secret."""

import base64

from ai_provider import DEFAULT_PROVIDER, SECRET_NAME, decode_profile, encode_profile, provider_from_payload
from common import CrawlError


PROFILE_ITEM = 'ai_provider.json'


def read(kube):
    path = f'/api/v1/namespaces/{kube.namespace}/secrets/{SECRET_NAME}'
    secret = kube.request('GET', path)
    encoded = (secret.get('data') or {}).get(PROFILE_ITEM)
    try:
        profile = decode_profile(base64.b64decode(encoded, validate=True)) if encoded else DEFAULT_PROVIDER
    except ValueError:
        raise CrawlError('The saved AI provider profile is invalid') from None
    return path, secret['metadata']['resourceVersion'], profile


def configure(kube, payload):
    path, version, prior = read(kube)
    provider = provider_from_payload(prior, payload)
    result = kube.request('PUT', path, {
        'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
        'metadata': {'name': SECRET_NAME, 'namespace': kube.namespace, 'resourceVersion': version},
        'data': {PROFILE_ITEM: base64.b64encode(encode_profile(provider).encode()).decode()},
    })
    if result is None:
        raise CrawlError('Provider settings changed. Reload them before saving')
    return provider.public()
