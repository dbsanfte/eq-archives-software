#!/usr/bin/env python3
"""Check compression and cache behavior against the actual NGINX container."""
import gzip
import re
import sys
import urllib.error
import urllib.request


def check(base):
    def get(path, headers=None):
        request = urllib.request.Request(base.rstrip('/') + path, headers=headers or {})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    status, headers, html = get('/')
    assert status == 200 and headers.get('Cache-Control') == 'no-cache', 'HTML must revalidate across deployments'
    assets = re.findall(r'(?:src|href)="(/static/[^\"]+\.(?:js|css))"', html.decode())
    assert assets and any(asset.endswith('.js') for asset in assets)
    for asset in assets:
        status, headers, compressed = get(asset, {'Accept-Encoding': 'gzip'})
        assert status == 200 and headers.get('Content-Encoding') == 'gzip', f'Asset is not compressed: {asset}'
        assert 'accept-encoding' in headers.get('Vary', '').lower()
        assert headers.get('Cache-Control') == 'public, max-age=31536000, immutable'
        status, plain_headers, plain = get(asset, {'Accept-Encoding': 'identity'})
        assert status == 200 and plain_headers.get('Content-Encoding') is None
        assert gzip.decompress(compressed) == plain, 'Compressed and plain assets differ'
        assert len(compressed) < len(plain) * 0.6, 'Compression did not reduce the bundle sufficiently'
        status, cached_headers, body = get(asset, {'If-None-Match': plain_headers['ETag']})
        assert status == 304 and not body and 'immutable' in cached_headers['Cache-Control']
        print(f'{asset}: {len(plain):,} -> {len(compressed):,} bytes; immutable cache and conditional GET verified.')
    for path in ('/index.html', '/mcp.html', '/chatgpt.html', '/manifest.json'):
        status, headers, body = get(path)
        assert status == 200 and headers.get('Cache-Control') == 'no-cache', f'Mutable asset must revalidate: {path}'
    for path in ('/static/js/missing.0123456789.js', '/static/missing.js'):
        status, headers, body = get(path)
        assert status == 404 and 'immutable' not in headers.get('Cache-Control', ''), 'Missing asset must not return/cache the SPA shell'
    print('Static compression, immutable fingerprinted assets, mutable-page revalidation and missing-asset checks passed.')


if __name__ == '__main__':
    check(sys.argv[1])
