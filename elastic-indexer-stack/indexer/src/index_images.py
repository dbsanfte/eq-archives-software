"""Create-only, checkpointed image backfill from an existing archive snapshot.

Only local raster bytes are sent to the CUDA vision companion. No Luna calls,
clones, source preparation, mappings changes, existing-document overwrites or
unbounded archive filesystem walks. Sources and prior Jobs remain immutable.
"""
import argparse
from dataclasses import asdict,dataclass
from datetime import datetime,timezone
import hashlib
import logging
import mimetypes
import os
from pathlib import Path
from itertools import chain

from elasticsearch import ConflictError
from indexer.archive_images import Inventory
from indexer.archive_handler import ArchiveHandler
from indexer.es_manager import ElasticsearchManager
from indexer.openai_manager import OpenAIManager
from indexer import gemma
from indexer.image_embeddings import MAX_BYTES,VERSION,ImageUnavailable,prepare_image
from reindex_text import source_path

LOG=logging.getLogger(__name__)


@dataclass
class Progress:
    created:int=0
    existing:int=0
    cached:int=0
    unsupported:int=0
    missing:int=0
    failed:int=0


def document(inventory,record_id,blob,embedder,progress):
    parts=record_id.split('/',3)
    if len(parts)!=4 or len(parts[2])!=14 or not parts[2].isdigit():
        raise ImageUnavailable('invalid_capture_identity')
    try:
        timestamp=datetime.strptime(parts[2],'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        raise ImageUnavailable('invalid_capture_identity') from None
    cached=inventory.cached(blob,VERSION)
    if cached is not None:
        progress.cached+=1
        visual=cached
    else:
        original=inventory.repo/record_id
        if any(p.is_symlink() for p in (original,*original.parents) if p!=inventory.repo and inventory.repo in p.parents):
            raise ValueError('Image source contains a symlink')
        path=source_path(inventory.repo,record_id)
        if path.is_file():
            size=path.stat().st_size
        else:
            try:size=int(inventory.git('cat-file','-s',blob))
            except ValueError:raise FileNotFoundError('Image source unavailable locally; no fetch attempted') from None
        if size>MAX_BYTES:raise ImageUnavailable('image_byte_limit')
        if path.is_file():
            with path.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
        else:
            raw=inventory.git('cat-file','blob',blob)
        if len(raw)>MAX_BYTES:raise ImageUnavailable('image_byte_limit')
        expected=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if expected!=blob:raise ValueError('Raster source differs from the pinned archive revision')
        image=prepare_image(raw)
        visual={'image_vector':embedder.embed_image(image),'image_embedding_sha256':image.source_sha256,
                'image_embedding_provenance':image.provenance()}
        gemma.vector(visual['image_vector'])
        inventory.cache(blob,VERSION,visual)
    gemma.vector(visual['image_vector'])
    return {'id':record_id,'title':Path(parts[3].split('?',1)[0]).name,'file_type':'image',
            'mime_type':mimetypes.guess_type(parts[3].split('?',1)[0])[0] or 'image/unknown',
            'domain_name':parts[1],'capture_date':timestamp,'last_indexed':datetime.now(timezone.utc).isoformat(),
            'url':ArchiveHandler()._convert_to_archive_url(record_id),'thumbnail':'thumbnails/image.webp',
            'embedding_model':gemma.MODEL,'image_embedding_version':VERSION,'image_embedding_status':'embedded',
            'archive_source_sha256':visual['image_embedding_sha256'],'archive_commit':inventory.revision,**visual}


def index_images(client,index,inventory,embedder,*,limit=0,hosts=None):
    progress=Progress();attempted=0
    work=chain([(None,inventory.retry_batches(hosts=hosts),0)],
               ((host,inventory.batches(host,tree,after),after) for host,tree,after in inventory.pending_hosts(hosts)))
    for host,batches,after in work:
        for batch,ordinal,complete in batches:
            if batch:
                response=client.mget(index=index,ids=[item[1] for item in batch],source=False)
                rows=response.get('docs',[])
                if (len(rows)!=len(batch) or {row.get('_id') for row in rows}!={item[1] for item in batch}
                        or any(row.get('error') for row in rows)):
                    raise ValueError('Incomplete existing-image lookup')
                existing={row['_id'] for row in rows if row.get('found')}
            else:existing=set()
            last=after
            for position,record_id,blob in batch:
                if limit and attempted>=limit:
                    if host is not None:inventory.checkpoint(host,last,False)
                    else:inventory.db.commit()
                    progress.missing=inventory.missing_sources()
                    return progress
                attempted+=1;last=position
                if record_id in existing:
                    progress.existing+=1
                    inventory.clear_gap(record_id)
                    continue
                try:
                    source=document(inventory,record_id,blob,embedder,progress)
                except ImageUnavailable as error:
                    inventory.gap(record_id,blob,str(error));progress.unsupported+=1
                except FileNotFoundError:
                    inventory.gap(record_id,blob,'source_unavailable');progress.missing+=1
                else:
                    try:
                        result=client.create(index=index,id=record_id,document=source)
                    except ConflictError:
                        progress.existing+=1
                    else:
                        if result.get('result')!='created':raise ValueError('Image create was not acknowledged')
                        progress.created+=1
                    inventory.clear_gap(record_id)
            if host is not None:inventory.checkpoint(host,ordinal,complete)
            else:inventory.db.commit()
            after=ordinal
            LOG.info('Image progress %s',asdict(progress))
    progress.missing=inventory.missing_sources()
    return progress


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path('/data/eq-archives'))
    parser.add_argument('--state',type=Path,default=Path('/image-state/inventory.sqlite'))
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--host',action='append')
    args=parser.parse_args()
    if args.limit<0:parser.error('Invalid image bound')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING);logging.getLogger('elastic_transport').setLevel(logging.WARNING)
    manager=ElasticsearchManager(manage_index=False)
    embedder=OpenAIManager(embedding_model_name=gemma.MODEL)
    inventory=Inventory(args.repo,args.state)
    try:
        progress=index_images(manager.get_client(),manager._index_name,inventory,embedder,limit=args.limit,hosts=args.host)
        LOG.info('Image pass finished %s',asdict(progress))
        return 1 if progress.failed or progress.missing else 0
    finally:inventory.close()


if __name__=='__main__':
    raise SystemExit(main())
