"""Create-only website-text recovery from a pinned, prepared archive snapshot.

An explicit host scope, local Git trees and a persistent SQLite cursor replace
filesystem walks. Existing documents and enrichment are never overwritten.
Extraction failures remain in the private gap ledger while later files advance.
No paid enrichment, source preparation, fetches or index/mapping mutations.
"""
import argparse
from dataclasses import asdict,dataclass
from datetime import datetime,timezone
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re

from bs4 import BeautifulSoup
import chardet
from elasticsearch import ApiError,ConflictError
from langchain_core.documents import Document
import magic

from indexer.archive_images import Inventory
from indexer.archive_handler import ArchiveHandler
from indexer.es_manager import ElasticsearchManager
from indexer.openai_manager import OpenAIManager
from indexer.source_encoding import decode_source
from indexer.html_extraction import WEBSITE_EXTRACTION_VERSION,website_markdown
from indexer import gemma,passages
from indexer.discussions import optional_catalog, metadata as discussion_metadata
from reindex_text import source_path

LOG=logging.getLogger(__name__)
MAX_BYTES=8*1024*1024
# Wayback routes such as fboard.showMessage are text candidates too. File
# extensions cannot establish page types; only skip obvious static assets and
# use the verified bytes/MIME for dynamic endpoints (including avatar.php).
NON_TEXT=re.compile(r'\.(?:png|jpe?g|gif|webp|bmp|tiff?|ico|svg|css|js|map|woff2?|ttf|eot|zip|gz|bz2|7z|rar|exe|dll|pdf|docx?|xlsx?|pptx?|mp3|wav|ogg|avi|mov|mp4|swf)$',re.I)


class TextPaths:
    @staticmethod
    def search(name):
        return not NON_TEXT.search(name.split('?',1)[0].rsplit('/',1)[-1])



class TextInventory(Inventory):
    pattern=TextPaths

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.discussions=optional_catalog()

    def close(self):
        if getattr(self,'discussions',None):self.discussions.close()
        super().close()

    def select_hosts(self,hosts):
        hosts=list(dict.fromkeys(hosts or []))
        if not hosts or any(not re.fullmatch(r'[a-zA-Z0-9_.:\-]{1,253}',h) for h in hosts):
            raise ValueError('An explicit literal archive-host scope is required')
        known={row[0] for row in self.db.execute('SELECT name FROM hosts')}
        if not set(hosts)<=known:raise ValueError('Selected host is absent from the pinned source snapshot')
        scope=json.dumps(sorted(hosts));saved=self.db.execute("SELECT value FROM metadata WHERE key='text_hosts'").fetchone()
        if saved and saved[0]!=scope:raise ValueError('Host scope changed; use a separate state directory')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('text_hosts',?)",(scope,));self.db.commit()
        return hosts

    def pending_hosts(self,hosts=None):
        pending={row[0]:row for row in super().pending_hosts(hosts)}
        for host in hosts or sorted(pending):
            if host in pending:yield pending[host]


@dataclass
class Progress:
    created:int=0
    existing:int=0
    unsupported:int=0
    unavailable:int=0
    failed:int=0
    passages:int=0
    remaining_gaps:int=0


class Unsupported(ValueError):pass


def source_bytes(inventory,record_id,blob):
    original=inventory.repo/record_id
    if any(p.is_symlink() for p in (original,*original.parents) if p!=inventory.repo and inventory.repo in p.parents):
        raise ValueError('Source path contains a symlink')
    path=source_path(inventory.repo,record_id)
    if path.is_file():size=path.stat().st_size
    else:
        try:size=int(inventory.git('cat-file','-s',blob))
        except ValueError:raise FileNotFoundError('Source unavailable locally; no fetch attempted') from None
    if size>MAX_BYTES:raise Unsupported('source_byte_limit')
    if path.is_file():
        with path.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
    else:raw=inventory.git('cat-file','blob',blob)
    if len(raw)>MAX_BYTES:raise Unsupported('source_byte_limit')
    if hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=blob:
        raise ValueError('Source differs from the pinned archive revision')
    return raw


def prepare_document(inventory,record_id,blob):
    parts=record_id.split('/',3)
    if len(parts)!=4 or not re.fullmatch(r'\d{14}',parts[2]):raise Unsupported('invalid_capture_identity')
    try:timestamp=datetime.strptime(parts[2],'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).isoformat()
    except ValueError:raise Unsupported('invalid_capture_identity') from None
    raw=source_bytes(inventory,record_id,blob)
    mime=magic.from_buffer(raw[:8192],mime=True)
    if not (mime.startswith('text/') or mime in ('application/xhtml+xml','application/xml','application/json')):
        raise Unsupported('unsupported_media:'+mime)
    decoded=decode_source(raw,detection=chardet.detect(raw[:10000]));text=decoded.text
    html_source=bool(re.search(r'<(?:!doctype|html|head|body|title|table|p|h[1-6])\b',text[:8192],re.I)) or mime in ('text/html','application/xhtml+xml','application/xml','text/xml')
    if html_source and mime not in ('application/xml','text/xml'):mime='text/html'
    archive=ArchiveHandler();url=archive._convert_to_archive_url(record_id)
    alternate=archive._strip_index_html_from_url(url)
    header=f'<b>Page URL:</b> {html.escape(url)}<br/>'
    if alternate!=url:header+=f'<b>Alternate Page URL:</b> {html.escape(alternate)}<br/>'
    reader=website_markdown(header+'<hr/>'+text) if html_source else website_markdown(header+'<hr/>')+'\n\n'+text
    soup=BeautifulSoup(text,'html.parser') if html_source else None
    title=soup.title.get_text(' ',strip=True) if soup and soup.title else Path(parts[3].split('?',1)[0]).name
    plan=passages.prepare(reader,record_id,title,html_source=text if html_source else None,mime_type=mime)
    passages.verify(reader,plan)
    source={'id':record_id,'title':plan.title or title,'file_type':'text','mime_type':mime,'text_full':reader,
            'domain_name':parts[1],'capture_date':timestamp,'url':url,'alternate_url':alternate,
            'thumbnail':archive._resolve_thumbnail_url(url=url,file_type='text'),
            'last_indexed':datetime.now(timezone.utc).isoformat(),'embedding_model':gemma.MODEL,
            'text_extraction_version':WEBSITE_EXTRACTION_VERSION,'text_chunking_version':passages.VERSION,
            'text_passage_provenance':{'version':passages.VERSION,'sources':[plan.provenance()],'decoding':decoded.provenance()},
            'archive_source_sha256':decoded.source_sha256,'archive_commit':inventory.revision}
    discussion=discussion_metadata(record_id,raw,url,catalog=getattr(inventory,'discussions',None))
    if discussion:source['discussion']=discussion
    return source,plan


def index_websites(client,index,inventory,embedder,*,hosts,limit=0,retry_failed=False):
    hosts=inventory.select_hosts(hosts);progress=Progress();attempted=0
    def work():
        if retry_failed:
            after=''
            while True:
                rows=inventory.db.execute('SELECT id,blob FROM gaps WHERE id>? ORDER BY id LIMIT 50',(after,)).fetchall()
                if not rows:break
                after=rows[-1][0];batch=[(i,key,blob) for i,(key,blob) in enumerate(rows,1) if key.startswith(tuple('websites/'+h+'/' for h in hosts))]
                if batch:yield None,batch,0,False
        for host,tree,after in inventory.pending_hosts(hosts):
            for batch,ordinal,complete in inventory.batches(host,tree,after):yield host,batch,ordinal,complete
    for host,batch,ordinal,complete in work():
        rows=client.mget(index=index,ids=[item[1] for item in batch],source=False).get('docs',[]) if batch else []
        if len(rows)!=len(batch) or {x.get('_id') for x in rows}!={item[1] for item in batch} or any(x.get('error') or type(x.get('found')) is not bool for x in rows):
            raise ValueError('Incomplete existing-document lookup')
        existing={x['_id'] for x in rows if x.get('found')}
        last=0
        for position,key,blob in batch:
            if limit and attempted>=limit:
                if host is not None and last:inventory.checkpoint(host,last,False)
                inventory.db.commit();progress.remaining_gaps=inventory.db.execute("SELECT COUNT(*) FROM gaps WHERE reason NOT LIKE 'unsupported_media:%'").fetchone()[0]
                return progress
            attempted+=1;last=position
            if key in existing:
                progress.existing+=1;inventory.clear_gap(key);continue
            try:source,plan=prepare_document(inventory,key,blob)
            except Unsupported as error:
                inventory.gap(key,blob,str(error));progress.unsupported+=1;continue
            except FileNotFoundError:
                inventory.gap(key,blob,'source_unavailable');progress.unavailable+=1;continue
            except (ValueError,RecursionError) as error:
                inventory.gap(key,blob,'extraction:'+type(error).__name__);progress.failed+=1
                LOG.error('Website extraction failed (%s): %s',type(error).__name__,key);continue
            chunks=embedder.get_chunks_and_embeddings(Document(page_content=source['text_full']),_prepared_passages=plan)
            if len(chunks)!=len(plan.passages) or any(row['text_chunk']!=p.text for row,p in zip(chunks,plan.passages)):
                raise ValueError('Incomplete source-bound embedding response')
            source['text']=[{**row,'vector':gemma.vector(list(map(float,row['vector'])))} for row in chunks]
            try:result=client.create(index=index,id=key,document=source)
            except ConflictError:progress.existing+=1
            except ApiError as error:
                if error.status_code not in (400,413):raise
                inventory.gap(key,blob,'indexing:HTTP'+str(error.status_code));progress.failed+=1
                LOG.error('Website indexing failed (HTTP %s): %s',error.status_code,key);continue
            else:
                if result.get('result')!='created':raise ValueError('Website create was not acknowledged')
                progress.created+=1;progress.passages+=len(chunks)
            inventory.clear_gap(key)
        if host is not None:inventory.checkpoint(host,ordinal,complete)
        else:inventory.db.commit()
        LOG.info('Website import host=%s ordinal=%s complete=%s progress %s',host,ordinal,complete,asdict(progress))
    progress.remaining_gaps=inventory.db.execute("SELECT COUNT(*) FROM gaps WHERE reason NOT LIKE 'unsupported_media:%'").fetchone()[0]
    return progress


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path('/data/eq-archives'))
    parser.add_argument('--state',type=Path,default=Path(os.environ.get('TEXT_IMPORT_STATE','/text-state/inventory.sqlite')))
    parser.add_argument('--host',action='append',default=None)
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--retry-failed',action='store_true')
    args=parser.parse_args();hosts=args.host or os.environ.get('TEXT_IMPORT_HOSTS','').split(',')
    if args.limit<0:parser.error('Invalid import bound')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING);logging.getLogger('elastic_transport').setLevel(logging.WARNING)
    inventory=TextInventory(args.repo,args.state)
    try:
        inventory.select_hosts(hosts)
        manager=ElasticsearchManager(manage_index=False);embedder=OpenAIManager(embedding_model_name=gemma.MODEL)
        progress=index_websites(manager.get_client(),manager._index_name,inventory,embedder,hosts=hosts,limit=args.limit,retry_failed=args.retry_failed)
        LOG.info('Website import finished %s',asdict(progress))
        return 1 if progress.remaining_gaps else 0
    finally:inventory.close()


if __name__=='__main__':raise SystemExit(main())
