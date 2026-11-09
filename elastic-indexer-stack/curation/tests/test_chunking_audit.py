"""The audit is a LAN-only, static receipt; opening it cannot start work."""
import json
from pathlib import Path
import pytest
from server import create_app
from state import connect
from test_server import call


@pytest.mark.parametrize('path,media',[
    ('/chunking-audit','text/html'),('/assets/chunking-audit.js','text/javascript'),
    ('/assets/chunking-audit.css','text/css'),('/assets/chunking-audit.json','application/json')])
def test_audit_routes_keep_intranet_security_and_do_not_start_operations(tmp_path,path,media):
    app=create_app(tmp_path,start_worker=False)
    response=call(app,'GET',path)
    assert response.status_code==200 and media in response.headers['content-type']
    assert response.headers['cache-control']=='no-store'
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert call(app,'GET',path,peer='203.0.113.10',headers={'X-Forwarded-For':'192.168.68.20'}).status_code==403
    assert call(app,'GET',path,headers={'Host':'evil.example'}).status_code==403
    with connect(tmp_path) as store:
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0


def test_audit_lists_every_site_once_in_capture_count_order():
    report=json.loads((Path(__file__).parent.parent/'chunking-audit.json').read_text())
    rows=report['sites']
    assert len(rows)==50 and len({r['site'] for r in rows})==50
    assert [r['rank'] for r in rows]==list(range(1,51))
    assert [r['pages'] for r in rows]==sorted((r['pages'] for r in rows),reverse=True)
    assert rows[0]['site']=='ezboard.com' and rows[-1]['site']=='ralloszek.net'
    assert all(r['status'] in ('fixed','covered','generic','pending') for r in rows)
