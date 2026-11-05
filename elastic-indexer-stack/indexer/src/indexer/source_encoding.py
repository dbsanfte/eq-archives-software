"""Shared, offline source decoding; no hostname, URL or corpus-specific rules."""
import codecs
from dataclasses import dataclass
import hashlib
import re

VERSION='source-decoding-v2'
KANA=re.compile(r'[\u3040-\u30ff]')
KANJI=re.compile(r'[\u4e00-\u9fff]')
HALF_KANA=re.compile(r'[\uff61-\uff9f]')
BAD=re.compile(r'[\udc80-\udcff]')
JAPANESE={'shift_jis','cp932','euc_jp','iso2022_jp'}


@dataclass(frozen=True)
class Decoded:
    text:str
    encoding:str
    invalid_bytes:int
    reason:str
    source_sha256:str
    punctuation_bytes:int=0
    segment_encodings:tuple=()
    regions:tuple=()

    def provenance(self):
        value={'version':VERSION,'encoding':self.encoding,'invalid_bytes':self.invalid_bytes,
               'reason':self.reason,'source_sha256':self.source_sha256}
        if self.punctuation_bytes:
            value['recoveries']=[{'encoding':'cp1252','kind':'isolated_ascii_punctuation',
                                  'bytes':self.punctuation_bytes}]
        if self.segment_encodings:
            value.setdefault('recoveries',[]).extend(
                {'encoding':encoding,'kind':'text_regions','segments':count}
                for encoding,count in self.segment_encodings)
        if self.regions:value['regions']=list(self.regions)
        return value


def canonical(name):
    try:return codecs.lookup(name).name
    except (LookupError,TypeError):return None


def repaired_title(previous,expected,decoded):
    """Repair only a title proven to be the source title misread as Western."""
    if not isinstance(previous,str) or not expected or previous==expected or decoded.reason not in ('japanese_script_evidence','regional_evidence'):
        return previous
    encodings=({r['encoding'] for r in decoded.regions} if decoded.encoding=='mixed' else {decoded.encoding})
    for western,encoding in ((w,e) for w in ('iso8859-1','cp1252') for e in encodings):
        try:
            if previous.encode(western).decode(encoding,errors='replace')==expected:
                # HTML text nodes retain their entire parent/sibling tree.
                # Return plain text so preparation results can be pickled.
                return str(expected)
        except UnicodeError:pass
    return previous


def mixed_punctuation(text):
    # Rejected Shift-JIS bytes can be Western list punctuation pasted into an
    # otherwise Japanese page. Only recover isolated dashes/bullets between
    # ASCII words/numbers; valid multibyte characters and damaged kana stay put.
    pattern=r'(?<=[A-Za-z0-9])([ \t]+)([\udc95-\udc97])([ \t]+)(?=[A-Za-z0-9])'
    return re.subn(pattern,lambda m:m[1]+chr(ord(m[2])-0xdc00).encode('latin1').decode('cp1252')+m[3],text)


def regional_decode(data,limit,base_encoding,*,hints=()):
    """Decode local byte regions using Unicode/script evidence and inheritance.

    ASCII markup and whitespace are safe boundaries for the supported legacy
    codecs. A text node supplies context for short/ambiguous words. Require
    independent evidence for multiple encodings before changing the file-level
    interpretation; retain bounded replacement markers for uncertain bytes.
    """
    html=bool(re.search(rb'<(?:!doctype|html|body)\b',data[:8192],re.I))
    pattern=rb'(<[^>]*>|[\r\n]+)' if html else rb'([\r\n]+)'
    nodes=re.split(pattern,data)
    def candidates(raw):
        result=[]
        codecs_to_try=list(dict.fromkeys(('utf-8','shift_jis','cp932','euc_jp',base_encoding,*hints)))
        if b'\x1b$' in raw:codecs_to_try.insert(0,'iso2022_jp')
        for encoding in codecs_to_try:
            try:text=raw.decode(encoding)
            except UnicodeDecodeError:continue
            kana=len(KANA.findall(text));kanji=len(KANJI.findall(text));half=len(HALF_KANA.findall(text))
            if encoding=='utf-8':
                high=sum(ord(c)>127 for c in text)
                if high:result.append((100 if high>=2 or any(ord(c)>2047 for c in text) else 1,high,encoding,text))
            elif encoding in JAPANESE and kana and kana*4>=kana+kanji+half:
                result.append((10 if kana>=2 and kana+kanji>=4 else 1,kana+kanji,encoding,text))
            elif encoding not in JAPANESE:
                scripts=((0x370,0x3ff),(0x400,0x52f),(0x590,0x5ff),(0x600,0x6ff),(0x3400,0x9fff),(0xac00,0xd7af))
                letters=max(sum(low<=ord(c)<=high for c in text) for low,high in scripts)
                if letters>=4:result.append((10,letters,encoding,text))
        if re.search(rb'[A-Za-z]{3}',raw) and any(b>=128 for b in raw):
            # Western words/punctuation, with no strong Japanese interpretation.
            # Undefined Windows bytes and controls cannot establish evidence.
            try:
                text=raw.decode('cp1252')
                if not any(c in '\x00\x1b' for c in text) and not any(c[0]>=10 for c in result):
                    result.append((10,0,'cp1252',text))
            except UnicodeDecodeError:pass
        return result
    regions=[];anchors=set();offset=0
    for i,node in enumerate(nodes):
        # Delimiters and markup inherit the file codec without voting.
        text_or_tag=not i%2 or node.startswith(b'<')
        encoded=not node.isascii() or b'\x1b' in node
        options=candidates(node) if text_or_tag and encoded else []
        parent=max(options,key=lambda c:c[:2]) if options else None
        if parent and parent[0]>=10:anchors.add('shift_jis' if parent[2]=='cp932' else parent[2])
        chunks=re.split(rb'([ \t]+)',node) if not i%2 else [node]
        for chunk in chunks:
            options=candidates(chunk) if text_or_tag and (not chunk.isascii() or b'\x1b' in chunk) else []
            best=max(options,key=lambda c:c[:2]) if options else None
            if best and best[0]>=10:
                anchors.add('shift_jis' if best[2]=='cp932' else best[2])
            regions.append((offset,chunk,parent,best));offset+=len(chunk)
    if len(anchors)<2:return None
    output=[];counts={};saved=[];invalid=0
    for start,raw,parent,best in regions:
        if raw.isascii() and b'\x1b' not in raw:output.append(raw.decode('ascii'));continue
        inherited=parent[2] if parent and parent[0]>=10 else base_encoding
        if best and best[0]>=10:
            encoding,text=best[2:];reason='local_evidence'
        else:
            # Short ambiguous runs follow their node's evidence. A strict
            # unique alternative can recover a short label in another codec.
            encoding=inherited;reason='inherited_context'
            try:text=raw.decode(encoding)
            except UnicodeDecodeError:
                if best:encoding,text=best[2:];reason='local_evidence'
                else:text=raw.decode(encoding,errors='surrogateescape')
        damaged=len(BAD.findall(text));invalid+=damaged
        output.append(BAD.sub('\ufffd',text));counts[encoding]=counts.get(encoding,0)+1
        if (saved and saved[-1]['encoding']==encoding and saved[-1]['reason']==reason
                and data[saved[-1]['raw_end']:start].isascii()):
            saved[-1]['raw_end']=start+len(raw);saved[-1]['invalid_bytes']+=damaged
        else:
            saved.append({'raw_start':start,'raw_end':start+len(raw),'encoding':encoding,
                          'reason':reason,'invalid_bytes':damaged})
    if invalid>limit:return None
    return ''.join(output),invalid,tuple(sorted(counts.items())),tuple(saved)


def decode_source(data,content_type='',*,detection=None):
    """Prefer Unicode, then source-backed legacy encodings over lossless gibberish.

    Old archive wrappers often declare UTF-8 around non-UTF-8 bytes. Validate the
    full source, not just that declaration or a detector's first 10KB. CP932 is a
    superset fallback for Windows Shift-JIS extensions. A few truncated archived
    sequences become visible U+FFFD markers, counted in provenance; never ignore
    bytes or convert an entire Japanese page to Latin-1 for one damaged byte.
    """
    source_hash=hashlib.sha256(data).hexdigest();byte_offset=0;declared=[]
    def result(text,encoding,reason,invalid=0,punctuation=0,segments=(),regions=()):
        if byte_offset:regions=tuple({**r,'raw_start':r['raw_start']+byte_offset,'raw_end':r['raw_end']+byte_offset} for r in regions)
        return Decoded(text,encoding,invalid,reason,source_hash,punctuation,segments,regions)
    def local(encoding,limit):
        recovered=regional_decode(data,limit,encoding,hints=declared)
        if recovered:
            text,invalid,segments,regions=recovered
            return result(text,'mixed','regional_evidence',invalid,segments=segments,regions=regions)
    for prefix,encoding in [(codecs.BOM_UTF32_LE,'utf-32'),(codecs.BOM_UTF32_BE,'utf-32'),
                            (codecs.BOM_UTF8,'utf-8-sig'),(codecs.BOM_UTF16_LE,'utf-16'),(codecs.BOM_UTF16_BE,'utf-16')]:
        if data.startswith(prefix):
            try:return result(data.decode(encoding),encoding,'byte_order_mark')
            except UnicodeDecodeError:
                if encoding!='utf-8-sig':raise
                data=data[len(prefix):];byte_offset=len(prefix)
    # ISO-2022-JP is seven-bit; decoding it as ASCII/UTF-8 would retain its
    # escape sequences rather than the actual Japanese characters.
    if re.search(rb'\x1b\$(?:@|B|\(D)',data):
        try:return result(data.decode('iso2022_jp'),'iso2022_jp','iso2022_escape')
        except UnicodeDecodeError:pass
    try:
        text=data.decode('utf-8')
        if b'\x1b$' in data:
            recovered=local('utf-8',min(64,max(2,sum(b>=128 for b in data)//50)))
            if recovered:return recovered
        return result(text,'utf-8','valid_utf8')
    except UnicodeDecodeError:pass
    declared=[]
    for blob in [content_type.encode('ascii','ignore'),data[:8192]]:
        for match in re.finditer(rb'(?:charset\s*=\s*[\"\x27]?|<\?xml[^>]*encoding\s*=\s*[\"\x27])([a-zA-Z0-9_.-]+)',blob,re.I):
            value=canonical(match[1].decode('ascii'))
            if value and value not in declared:declared.append(value)
    detection=detection or {}
    detected=canonical(detection.get('encoding'))
    hints=[c for c in declared+[detected] if c in JAPANESE]
    # Require actual Japanese script evidence. Strict CP932 alone can decode
    # some Western bytes too, so mere codec success is insufficient.
    candidates=[];damaged_japanese=False
    high=sum(byte>=128 for byte in data)
    for encoding in list(dict.fromkeys(hints+['shift_jis','cp932','euc_jp'])):
        text=data.decode(encoding,errors='surrogateescape')
        invalid=len(BAD.findall(text));kana=len(KANA.findall(text));kanji=len(KANJI.findall(text));half=len(HALF_KANA.findall(text))
        hinted=encoding in hints or encoding=='cp932' and 'shift_jis' in hints
        enough=(kana>=2 or kanji>=4) if hinted else kana>=8 and kana+kanji>=16 and kana*4>=kana+kanji+half
        limit=min(64,max(2,high//50));punctuation=0
        if enough and invalid>limit:
            recovered,count=mixed_punctuation(text)
            if count and invalid-count<=limit:
                text=recovered;invalid-=count;punctuation=count
        if enough and invalid<=limit:
            candidates.append((invalid,-kana,encoding,text,punctuation))
        elif enough and invalid<=high*.2:
            damaged_japanese=True
    if candidates:
        # Prefer strict decoding, then more recovered Japanese. Stable ordering
        # keeps standard Shift-JIS code points when CP932 is unnecessary.
        invalid,_,encoding,text,punctuation=min(candidates,key=lambda row:row[:2])
        return local(encoding,limit) or result(BAD.sub('\ufffd',text),encoding,'japanese_script_evidence',invalid,punctuation)
    if damaged_japanese:
        recovered=local('cp932',limit)
        if recovered:return recovered
        raise UnicodeError('Japanese source exceeds bounded decoding damage; source bytes retained')
    # Legacy HTML labelled Latin-1 commonly contains Windows smart punctuation.
    # Do not turn the previously readable punctuation into C1 control characters.
    if 'iso8859-1' in declared and ('html' in content_type.lower() or re.search(rb'<(?:!doctype|html|body)\b',data[:8192],re.I)):
        declared.insert(declared.index('iso8859-1'),'cp1252')
    for encoding in list(dict.fromkeys(declared+([detected] if detected else [])+['cp1252','iso8859-1'])):
        try:
            text=data.decode(encoding)
            return local(encoding,limit) or result(text,encoding,'legacy_fallback')
        except (UnicodeDecodeError,LookupError):continue
    raise UnicodeError('Could not decode source')
