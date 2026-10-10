#!/usr/bin/env python3
"""Read-only smoke check for public Explore routes; no archive writes or AI calls."""
import argparse
import json
import urllib.parse
import urllib.request


def check(base_url, host=None):
    def get(kind, selection=None):
        headers = {'Host': host} if host else {}
        url = base_url.rstrip('/') + '/api/explore/' + kind
        if selection:
            url += '?' + urllib.parse.urlencode(selection)
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.headers['Content-Type'].startswith('application/json'), 'Explore API reached the SPA fallback'
            assert response.headers['Cache-Control'] == 'no-store'
            return json.load(response)
    overview = get('overview')
    assert overview['records'] > 0 and overview['sites_count'] > 0
    assert overview['timeline'] and overview['sites']
    cloud = get('phrases')
    assert 0 < cloud['sampled_pages'] <= cloud['sample_limit'] <= 100
    assert cloud['phrases'] and len(cloud['phrases']) <= 40
    assert overview['selection'] == cloud['selection']
    assert 'text_full' not in json.dumps(cloud)
    selection = {**cloud['selection'], 'phrase': cloud['phrases'][0]['text']}
    focused = get('overview', selection)
    assert focused['selection'] == selection and focused['records'] > 0, 'Cloud phrase has no matching source captures'
    print('Explore overview, bounded phrases and a source phrase drill-down are available through public routing.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('base_url')
    parser.add_argument('--host')
    args = parser.parse_args()
    check(args.base_url, args.host)
