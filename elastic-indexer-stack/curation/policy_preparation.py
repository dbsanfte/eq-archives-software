"""CPU-only maintenance plans. No credentials, network clients or state writers."""
from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
import html
import importlib.metadata
import inspect
import json
import os
import re
import sys

from bs4 import BeautifulSoup

from indexer import gemma, passages, html_extraction
from indexer.archive_handler import ArchiveHandler
from indexer.source_encoding import VERSION as DECODING_VERSION, decode_source, repaired_title
from indexer.source_files import SourceError, relative

LEGACY_POLICY='embeddinggemma2-paragraphs-480-overlap48-v1'
FIELDS=('text_chunk','title','context','kind','source_start','source_end')
# These extraction checkpoints used this exact reader renderer. Source bytes,
# decoded HTML, reader and decoder provenance must also match. A renderer or
# dependency change automatically disables this migration compatibility path.
LEGACY_RENDERER='f7f14f134c636a06058e872f252ae1640aba99b8d59ca0ef81cda768e99e6693'
LEGACY_REQUEST='bc3b052359bb7cf79b8b4a40d8ea96dac78ff5d9b5a97cf54517adb892640bab'
READER_CHECKPOINTS={'html-2026-11-05-regional-decoding','html-2026-11-05-script-evidence',
                    'html-2026-11-12-jis-extensions'}
TITLE_REPAIR_REASONS = ('japanese_script_evidence', 'regional_evidence', 'iso2022_escape')
TEXTUAL_MIME = ('application/xhtml+xml', 'application/xml', 'application/json')


@lru_cache(maxsize=1)
def renderer_identity():
    parts=[inspect.getsource(value) for value in (html_extraction.is_layout_table,
        html_extraction.website_markdown,html_extraction.flatten_deep_markup,
        html_extraction.collapse_long_whitespace,html_extraction.SourceMarkdownConverter,
        ArchiveHandler.__init__,ArchiveHandler._convert_to_archive_url,ArchiveHandler._strip_index_html_from_url,
        website_reader,html.escape)]
    versions={name:importlib.metadata.version(name) for name in ('beautifulsoup4','markdownify')}
    handler=ArchiveHandler()
    roots=[handler._websites_path,handler._mailing_lists_path,handler._newsgroups_path]
    return hashlib.sha256(json.dumps([parts,versions,list(sys.version_info[:3]),os.name,roots],sort_keys=True).encode()).hexdigest()


@lru_cache(maxsize=1)
def request_identity():
    parts=[inspect.getsource(value) for value in (passages._plain,passages._short,passages._short_cached,
        passages.document_prefix,passages.Passage.request.fget)]
    return hashlib.sha256(json.dumps([parts,gemma.MODEL,gemma.TOKENIZER_SHA256,passages.MAX_TOKENS,
        importlib.metadata.version('tokenizers'),list(sys.version_info[:3])],sort_keys=True).encode()).hexdigest()


def website_reader(raw,identifier):
    archive=ArchiveHandler();url=archive._convert_to_archive_url(identifier)
    alternate=archive._strip_index_html_from_url(url)
    header='<b>Page URL:</b> '+html.escape(url)+'<br/>'
    if alternate and alternate!=url:header+='<b>Alternate Page URL:</b> '+html.escape(alternate)+'<br/>'
    return html_extraction.website_markdown(header+'<hr/>'+raw)


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


def decoded_website(sources, source, mime, previous):
    hint = previous.get('encoding')
    detected = {'encoding': hint, 'confidence': 1.0} if hint else None
    decoded = decode_source(sources.read(source), mime, detection=detected)
    return replace(decoded, text=decoded.text.replace('\r\n', '\n').replace('\r', '\n'))


def rebuild_view(rows):
    """Recover the selected source in offset order, proving every overlap."""
    if not isinstance(rows, list) or not rows:
        return None
    view = ''
    previous_start = -1
    for row in rows:
        if not isinstance(row, dict):
            return None
        start, end, body = (row.get(key) for key in ('source_start', 'source_end', 'text_chunk'))
        covered = len(view)
        if (type(start) is not int or type(end) is not int or not isinstance(body, str)
                or not previous_start < start <= covered <= end <= 32 * 1024 * 1024
                or start == end or len(body) != end - start
                or view[start:] != body[:covered - start]):
            return None
        view += body[covered - start:]
        previous_start = start
    return view


def enrichment_from_checkpoint(source, sources):
    """Reuse a current indexed view for grounding without rebuilding body chunks."""
    previous = source.get('text_passage_provenance') or {}
    if (source.get('text_chunking_version') != passages.VERSION
            or not isinstance(previous, dict) or previous.get('version') != passages.VERSION):
        return None
    bound = bindings(previous)
    if not isinstance(bound, list) or len(bound) != 1 or not isinstance(bound[0], dict):
        return None
    binding = bound[0]
    reader = source.get('text_full')
    if (binding.get('version') != passages.VERSION or not isinstance(reader, str) or not reader.strip()
            or binding.get('source_sha256') != passages.digest(reader)
            or binding.get('method') == 'archive_status'
            or 'supporting_asset' in (binding.get('exclusions') or ())):
        return None
    identifier = source['id']
    relative(identifier)
    view = rebuild_view(source.get('text'))
    if not view or passages.digest(view) != binding.get('view_sha256'):
        return None
    if not identifier.startswith('websites/'):
        if binding.get('method') == 'source_text' and binding.get('html_sha256') is None:
            return reader, binding['source_sha256'], view
        return None
    mime = source.get('mime_type') or ''
    decoding = previous.get('decoding') or {}
    if not isinstance(decoding, dict) or decoding.get('version') != DECODING_VERSION:
        return None
    source_hash = decoding.get('source_sha256')
    if (not (isinstance(mime, str) and (not mime or mime.startswith('text/') or mime in TEXTUAL_MIME))
            or source.get('text_extraction_version') != html_extraction.WEBSITE_EXTRACTION_VERSION
            or not isinstance(source_hash, str) or not re.fullmatch('[0-9a-f]{64}', source_hash)
            or not isinstance(binding.get('html_sha256'), str)):
        return None
    decoded = decoded_website(sources, source, mime, decoding)
    raw = decoded.text
    if (decoded.reason in TITLE_REPAIR_REASONS or decoded.source_sha256 != source_hash
            or passages.digest(raw) != binding['html_sha256']):
        return None
    body = raw if mime == 'text/plain' else html_extraction.website_markdown(raw)
    return body, decoded.source_sha256, view


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
    enrichment_source:tuple[str,str] | None=None


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


def prepare(hit,sources,*,enrichment=False):
    """Return a complete source-bound plan without inference or writes."""
    source=hit['_source'];identifier=hit['_id'];reader=source.get('text_full') or ''
    try:return _prepare(source,identifier,reader,sources,enrichment)
    except SourceError as error:
        if not error.source_sha256:
            error.source_sha256=((source.get('text_passage_provenance') or {}).get('decoding') or {}).get('source_sha256') or source.get('archive_source_sha256')
        raise
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,MemoryError) as error:
        hashed=((source.get('text_passage_provenance') or {}).get('decoding') or {}).get('source_sha256') or source.get('archive_source_sha256')
        raise SourceError(getattr(error,'reason','source_validation'),hashed) from None


def _prepare(source,identifier,reader,sources,enrichment):
    previous=source.get('text_passage_provenance') or {};version=source.get('text_chunking_version') or ''
    if source.get('embedding_model')!=gemma.MODEL or source.get('file_type')!='text':
        return Prepared(identifier,reader,None,{},result='different_model')
    if version and newer(version):return Prepared(identifier,reader,None,{},result='newer_policy')
    relative(identifier)
    website=identifier.startswith('websites/');mime=source.get('mime_type') or ''
    textual=not mime or mime.startswith('text/') or mime in TEXTUAL_MIME
    if not reader.strip():raise SourceError('reader_unavailable')
    bound=bindings(previous);legacy_reader=version==LEGACY_POLICY and not previous and website and textual
    if not legacy_reader and (len(bound)!=1 or bound[0].get('source_sha256')!=passages.digest(reader)):
        raise SourceError('reader_integrity')
    complete=version==passages.VERSION and previous.get('version')==passages.VERSION and (not website or not textual or source.get('text_extraction_version')==html_extraction.WEBSITE_EXTRACTION_VERSION)
    patch={};raw=None;decoding=None;proof=None
    if website and textual:
        decoded=decoded_website(sources,source,mime,previous.get('decoding') or {})
        raw=decoded.text;decoding=decoded.provenance()
        if decoded.reason in TITLE_REPAIR_REASONS:
            title=BeautifulSoup(raw,'html.parser').title
            corrected=repaired_title(source.get('title'),title.string if title else None,decoded)
            if corrected!=source.get('title'):patch['title']=corrected
        if legacy_reader or source.get('text_extraction_version')!=html_extraction.WEBSITE_EXTRACTION_VERSION:
            if legacy_reader or not reader_reusable(source,reader,raw,decoding):
                reader=website_reader(raw,identifier)
                if not reader.strip():raise SourceError('reader_integrity')
                if reader!=source['text_full']:patch['text_full']=reader
            patch['text_extraction_version']=html_extraction.WEBSITE_EXTRACTION_VERSION
        proof=reader_proof(reader,raw,decoding)
    plan=passages.prepare(reader,identifier,patch.get('title',source.get('title')) or '',html_source=raw,mime_type=mime)
    reuse=not legacy_reader and plan_reusable(source,reader,plan)
    if complete and not patch:
        if enrichment:
            # Paid work needs the stored projection and reader to agree. It
            # does not claim the existing vectors are reusable after inference
            # runtime changes, since it never writes the body vectors.
            if len(bound)!=1 or bound[0].get('view_sha256')!=passages.digest(plan.view):
                raise SourceError('checkpoint_integrity')
            if not plan.passages or (website and not textual):
                return Prepared(identifier,reader,plan,{},result='external_current')
            if raw is not None:
                body=raw if mime=='text/plain' else html_extraction.website_markdown(raw)
                source_hash=decoding['source_sha256']
            else:
                body,source_hash=reader,plan.source_sha256
            return Prepared(identifier,reader,plan,{},result='external_current',enrichment_source=(body,source_hash))
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
