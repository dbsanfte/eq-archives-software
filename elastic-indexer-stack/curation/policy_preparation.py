"""CPU-only maintenance plans. No credentials, network clients or state writers."""
from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
import html
import importlib.metadata
import inspect
import json
import re

from bs4 import BeautifulSoup

from indexer import gemma, passages, html_extraction
from indexer.archive_handler import ArchiveHandler
from indexer.source_encoding import decode_source, repaired_title
from indexer.source_files import SourceError, relative

LEGACY_POLICY='embeddinggemma2-paragraphs-480-overlap48-v1'
FIELDS=('text_chunk','title','context','kind','source_start','source_end')
# These extraction checkpoints used this exact reader renderer. Source bytes,
# decoded HTML, reader and decoder provenance must also match. A renderer or
# dependency change automatically disables this migration compatibility path.
LEGACY_RENDERER='a2f9e3461a3f550693a3b4226c82c92c540d413c05edf9e46fbc288f831c937c'
LEGACY_REQUEST='a5d1fa645c9b49ae12be429b281d116e32d6b9b45eeb22d8756a06f8e0e590b5'
READER_CHECKPOINTS={'html-2026-11-05-regional-decoding','html-2026-11-05-script-evidence',
                    'html-2026-11-12-jis-extensions'}


@lru_cache(maxsize=1)
def renderer_identity():
    parts=[inspect.getsource(value) for value in (html_extraction.is_layout_table,
        html_extraction.website_markdown,html_extraction.SourceMarkdownConverter,
        ArchiveHandler._convert_to_archive_url,ArchiveHandler._strip_index_html_from_url)]
    versions={name:importlib.metadata.version(name) for name in ('beautifulsoup4','markdownify')}
    return hashlib.sha256(json.dumps([parts,versions],sort_keys=True).encode()).hexdigest()


@lru_cache(maxsize=1)
def request_identity():
    parts=[inspect.getsource(value) for value in (passages._short,passages._short_cached,
        passages.document_prefix,passages.Passage.request.fget)]
    return hashlib.sha256(json.dumps([parts,gemma.MODEL,gemma.TOKENIZER_SHA256,passages.MAX_TOKENS],sort_keys=True).encode()).hexdigest()


def newer(version):
    if version==LEGACY_POLICY:return False
    old=re.fullmatch(r'eq-passages-480-v(\d+)',version or '')
    current=re.fullmatch(r'eq-passages-480-v(\d+)',passages.VERSION)
    return not old or not current or int(old[1])>int(current[1])


def bindings(previous):
    return previous.get('sources') or [{k:v for k,v in previous.items() if k!='decoding'}]


def request_compatible(previous,version):
    if not re.fullmatch(r'eq-passages-480-v1[0-3]',version):return False
    profile=previous.get('request_profile')
    return profile==request_identity() if profile is not None else request_identity()==LEGACY_REQUEST


def plan_reusable(source,reader,plan):
    previous=source.get('text_passage_provenance') or {};bound=bindings(previous)
    version=source.get('text_chunking_version') or ''
    return (request_compatible(previous,version) and previous.get('version')==version and len(bound)==1
        and bound[0].get('version')==version and reader==source.get('text_full')
        and [(p.text,p.title,p.context,p.kind,p.start,p.end) for p in plan.passages]==
            [tuple(row.get(k) for k in FIELDS) for row in source.get('text') or []]
        and {k:v for k,v in bound[0].items() if k!='version'}=={k:v for k,v in plan.provenance().items() if k!='version'})


def reader_reusable(source,reader,raw,decoding):
    previous=source.get('text_passage_provenance') or {};bound=bindings(previous)
    if (len(bound)!=1 or bound[0].get('source_sha256')!=passages.digest(reader)
            or bound[0].get('html_sha256')!=passages.digest(raw)):
        return False
    proof=previous.get('reader') or {}
    if proof==reader_proof(reader,raw,decoding):return True
    return (renderer_identity()==LEGACY_RENDERER and source.get('text_extraction_version') in READER_CHECKPOINTS
        and (previous.get('decoding') or {}).get('version') in ('source-decoding-v2','source-decoding-v3','source-decoding-v4')
        and {k:v for k,v in (previous.get('decoding') or {}).items() if k!='version'}==
            {k:v for k,v in decoding.items() if k!='version'})


def reader_proof(reader,raw,decoding):
    return {'renderer':renderer_identity(),'reader_sha256':passages.digest(reader),
            'html_sha256':passages.digest(raw),'source_sha256':decoding['source_sha256']}


@dataclass(frozen=True)
class Prepared:
    identifier:str
    reader:str
    plan:passages.Plan | None
    patch:dict
    reuse:bool=False
    result:str | None=None
    requests:tuple=()
    positions:tuple=()
    reusable:tuple=()


def old_requests(source,reader,plan):
    """Prove the old selected view and full inputs before considering vectors.

    A changed projection can retain individual inputs. Reconstruct the old view
    from its checked offsets/overlaps, then bind both views to the same reader
    and decoded source. Unknown or damaged historical metadata simply reembeds.
    """
    previous=source.get('text_passage_provenance') or {};bound=bindings(previous)
    version=source.get('text_chunking_version','')
    if (not request_compatible(previous,version)
            or previous.get('version')!=version or len(bound)!=1
            or bound[0].get('version')!=version or reader!=source.get('text_full')
            or bound[0].get('source_sha256')!=passages.digest(reader)
            or bound[0].get('html_sha256')!=plan.html_sha256):return ()
    view='';old=[];requests=[]
    try:
        for index,row in enumerate(source.get('text') or []):
            body,title,context,kind,start,end=(row.get(k) for k in FIELDS)
            if (not all(isinstance(v,str) for v in (body,title,context,kind))
                    or type(start) is not int or type(end) is not int
                    or not 0<=start<end<=32*1024*1024 or len(body)!=end-start
                    or start>len(view) or end<len(view)
                    or view[start:]!=body[:len(view)-start]):return ()
            view+=body[len(view)-start:]
            passage=passages.Passage(start,end,body,title,context,kind)
            old.append(passage);requests.append((passage.request,index))
        if passages.digest(view)!=bound[0].get('view_sha256'):return ()
        old_plan=passages.Plan(passages.digest(reader),plan.html_sha256,view,'',
                              bound[0].get('method',''),(),tuple(old))
        passages.verify(reader,old_plan)
    except (ValueError,TypeError,KeyError):return ()
    return tuple(requests)


def prepare(hit,sources):
    """Return a complete source-bound plan without inference or writes."""
    source=hit['_source'];identifier=hit['_id'];reader=source.get('text_full') or ''
    try:return _prepare(source,identifier,reader,sources)
    except SourceError as error:
        if not error.source_sha256:
            error.source_sha256=((source.get('text_passage_provenance') or {}).get('decoding') or {}).get('source_sha256') or source.get('archive_source_sha256')
        raise
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,MemoryError) as error:
        hashed=((source.get('text_passage_provenance') or {}).get('decoding') or {}).get('source_sha256') or source.get('archive_source_sha256')
        raise SourceError(getattr(error,'reason','source_validation'),hashed) from None


def _prepare(source,identifier,reader,sources):
    previous=source.get('text_passage_provenance') or {};version=source.get('text_chunking_version') or ''
    if source.get('embedding_model')!=gemma.MODEL or source.get('file_type')!='text':
        return Prepared(identifier,reader,None,{},result='different_model')
    if version and newer(version):return Prepared(identifier,reader,None,{},result='newer_policy')
    relative(identifier)
    website=identifier.startswith('websites/');mime=source.get('mime_type') or ''
    textual=not mime or mime.startswith('text/') or mime in ('application/xhtml+xml','application/xml','application/json')
    if not reader.strip():raise SourceError('reader_unavailable')
    bound=bindings(previous);legacy_reader=version==LEGACY_POLICY and not previous and website and textual
    if not legacy_reader and (len(bound)!=1 or bound[0].get('source_sha256')!=passages.digest(reader)):
        raise SourceError('reader_integrity')
    complete=version==passages.VERSION and previous.get('version')==passages.VERSION and (not website or not textual or source.get('text_extraction_version')==html_extraction.WEBSITE_EXTRACTION_VERSION)
    patch={};raw=None;decoding=None;proof=None
    if website and textual:
        data=sources.read(source);hint=(previous.get('decoding') or {}).get('encoding')
        decoded=decode_source(data,mime,detection={'encoding':hint,'confidence':1.0} if hint else None)
        decoded=replace(decoded,text=decoded.text.replace('\r\n','\n').replace('\r','\n'))
        raw=decoded.text;decoding=decoded.provenance()
        if decoded.reason in ('japanese_script_evidence','regional_evidence','iso2022_escape'):
            title=BeautifulSoup(raw,'html.parser').title
            corrected=repaired_title(source.get('title'),title.string if title else None,decoded)
            if corrected!=source.get('title'):patch['title']=corrected
        if legacy_reader or source.get('text_extraction_version')!=html_extraction.WEBSITE_EXTRACTION_VERSION:
            if legacy_reader or not reader_reusable(source,reader,raw,decoding):
                archive=ArchiveHandler();url=archive._convert_to_archive_url(identifier)
                alternate=archive._strip_index_html_from_url(url)
                header='<b>Page URL:</b> '+html.escape(url)+'<br/>'
                if alternate and alternate!=url:header+='<b>Alternate Page URL:</b> '+html.escape(alternate)+'<br/>'
                reader=html_extraction.website_markdown(header+'<hr/>'+raw)
                if not reader.strip():raise SourceError('reader_integrity')
                if reader!=source['text_full']:patch['text_full']=reader
            patch['text_extraction_version']=html_extraction.WEBSITE_EXTRACTION_VERSION
        proof=reader_proof(reader,raw,decoding)
    plan=passages.prepare(reader,identifier,patch.get('title',source.get('title')) or '',html_source=raw,mime_type=mime)
    reuse=not legacy_reader and plan_reusable(source,reader,plan)
    if complete and not patch:
        if not reuse:raise SourceError('checkpoint_integrity')
        return Prepared(identifier,reader,plan,{},reuse=True,result='external_current')
    provenance={'version':passages.VERSION,'sources':[plan.provenance()],'request_profile':request_identity()}
    if decoding:provenance['decoding']=decoding
    if proof:provenance['reader']=proof
    patch.update(text_chunking_version=passages.VERSION,text_passage_provenance=provenance)
    if plan.method=='archive_status' or 'supporting_asset' in plan.exclusions:
        patch.update(llm_summary_vector=None,llm_image_text_vector=None)
    unique,positions=passages.embedding_inputs(plan)
    return Prepared(identifier,reader,plan,patch,reuse,requests=tuple(p.request for p in unique),
                    positions=positions,reusable=old_requests(source,reader,plan) if not reuse else ())


def verify_prepared(hit,prepared):
    if prepared.identifier!=hit['_id']:raise SourceError('preparation_identity')
    if prepared.plan is None:
        if prepared.result not in ('different_model','newer_policy'):raise SourceError('preparation_identity')
        return
    if prepared.reader!=prepared.patch.get('text_full',hit['_source'].get('text_full')):
        raise SourceError('preparation_reader')
    passages.verify(prepared.reader,prepared.plan)
    if prepared.reuse and not plan_reusable(hit['_source'],prepared.reader,prepared.plan):
        raise SourceError('preparation_reuse')
    if not prepared.result:
        unique,positions=passages.embedding_inputs(prepared.plan)
        if prepared.requests!=tuple(p.request for p in unique) or prepared.positions!=positions:
            raise SourceError('preparation_inputs')
        if not prepared.reuse and prepared.reusable!=old_requests(hit['_source'],prepared.reader,prepared.plan):
            raise SourceError('preparation_reuse')
