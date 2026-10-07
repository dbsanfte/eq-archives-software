"""Isolated browser/container fixture; never reads production keys or networks."""

import json
import os
from pathlib import Path

from common import capture_scope, digest
from captures import archive_path
from grading import SIGNATURE, sources
from review import record
from state import connect


def fixture(root):
    root=Path(root)
    with connect(root) as store:
        if store.candidates():
            return
        for name,grade in (('guild',3),('class',2),('mixed',1),('captured-guild',3),('captured-class',3)):
            url=f'http://{name}.example/eq/news.html'
            identifier=digest(url)[:24]
            captures=[]
            for timestamp,when in (('20000101000000','Early'),('20010101000000','Later')):
                body=(f'<title>{name} EQ archive</title><p>{when} EverQuest guild history.</p>'
                      f'<p>{name} site content.</p>'
                      '<p>Final paragraph.</p><p>&lt;script&gt;window.pwned=true&lt;/script&gt;</p>'
                      '<img src="https://external.example/never-load.jpg">').encode()
                path=root/'captures'/identifier/(timestamp+'.html')
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(body)
                captures.append({'url':url,'timestamp':timestamp,'path':str(path.relative_to(root)),
                                 'sha256':digest(body),'bytes':len(body),'tier':1,'title':name+' EQ archive','source':'wayback'})
            store.db.execute("INSERT INTO candidates(id,url,scope,priority,coverage,evidence,captures,state) VALUES (?,?,?,100,'{}','[]',?,'approval_pending')",
                             (identifier,url,capture_scope(url),json.dumps(captures)))
            current=store.db.execute('SELECT * FROM candidates WHERE id=?',(identifier,)).fetchone()
            rating={'grade':grade,'category':'guild','confidence':'high','origin':'model',
                    'reason':'Contemporary EQ information. <script>window.pwned=true</script>',
                    'evidence':[{'slot':0,'excerpt':'Early EverQuest guild history.'}],
                    'signature':digest({'grader':SIGNATURE,'documents':sources(store,current,120000)})}
            store.db.execute('UPDATE candidates SET rating=? WHERE id=?',(json.dumps(rating),identifier))
        store.db.commit()
        rows=[record(store,row) for row in store.candidates() if 'captured-' in row['url']]
        manifest={'schema':1,'batch_id':'a'*32,'sites':[{key:row[key] for key in ('id','url','scope','scope_mode','manifest_sha256')} for row in rows],
                  'captures':[{**capture,'candidate_id':row['id'],'archive_path':archive_path(capture)} for row in rows for capture in row['captures']]}
        guild=next(row for row in rows if 'captured-guild' in row['url'])
        raw=b'<title>Guild child guide</title><p>EverQuest guild child guide.</p><p>Last child paragraph.</p>'
        child_path=root/'captures'/guild['id']/'guide.html'
        child_path.write_bytes(raw)
        child={**guild['captures'][0],'candidate_id':guild['id'],'url':'http://captured-guild.example/eq/guide.html','path':str(child_path.relative_to(root)),
               'bytes':len(raw),'sha256':digest(raw),'title':'Guild child guide'}
        child['archive_path']=archive_path(child)
        manifest['captures'].append(child)
        store.db.execute("INSERT INTO batches VALUES (?,'awaiting_review',?,?,NULL,NULL,NULL,'now','now')",('a'*32,json.dumps(manifest),digest(manifest)))
        for row in rows:
            store.db.execute("UPDATE candidates SET state='captured_awaiting_review' WHERE id=?",(row['id'],))
        store.db.commit()


if __name__=='__main__':
    import uvicorn
    from server import create_app
    address=os.environ['LAN_BIND_IP']
    root=os.environ.get('CURATION_ROOT','/data')
    fixture(root)
    uvicorn.run(create_app(root,origin=f'http://{address}:8090',start_worker=False),host=address,port=8090,
                proxy_headers=False,access_log=False,log_level='warning')
