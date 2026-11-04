"""Apply additive embedding fields before rolling out image-aware queries.

Read the existing admin Kubernetes Secret from stdin. Never print its contents.
No archive documents, existing mappings, source checkouts or Jobs are modified.
"""
import base64
import json
import os
from pathlib import Path
import sys
import urllib.request


def main():
    secret=json.load(sys.stdin)
    password=base64.b64decode(secret['data']['ELASTIC_PASSWORD']).decode().strip()
    schema=json.loads(Path(__file__).with_name('schema.json').read_text())
    request=urllib.request.Request(os.environ['EMBEDDING_SCHEMA_ES_URL'].rstrip('/')+'/eq-archive/_mapping',
        method='PUT',data=json.dumps(schema).encode(),headers={'Content-Type':'application/json',
        'Authorization':'Basic '+base64.b64encode(('elastic:'+password).encode()).decode()})
    try:
        with urllib.request.urlopen(request,timeout=30) as response:
            if json.load(response).get('acknowledged') is not True:
                raise ValueError('Unacknowledged additive mapping')
    except (OSError,ValueError):
        raise SystemExit('Embedding schema provisioning failed; upstream details omitted') from None
    print('Verified additive visual-vector and passage-provenance mappings.')


if __name__=='__main__':
    main()
