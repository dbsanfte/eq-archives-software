"""Shared, offline source decoding; no hostname, URL or corpus-specific rules."""
import codecs
from dataclasses import dataclass
import hashlib
import re

VERSION='source-decoding-v1'
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

    def provenance(self):
        return {'version':VERSION,'encoding':self.encoding,'invalid_bytes':self.invalid_bytes,
                'reason':self.reason,'source_sha256':self.source_sha256}


def canonical(name):
    try:return codecs.lookup(name).name
    except (LookupError,TypeError):return None


def repaired_title(previous,expected,decoded):
    """Repair only a title proven to be the source title misread as Western."""
    if not isinstance(previous,str) or not expected or previous==expected or decoded.reason!='japanese_script_evidence':
        return previous
    for western in ('iso8859-1','cp1252'):
        try:
            if previous.encode(western).decode(decoded.encoding,errors='replace')==expected:
                return expected
        except UnicodeError:pass
    return previous


def decode_source(data,content_type='',*,detection=None):
    """Prefer Unicode, then source-backed legacy encodings over lossless gibberish.

    Old archive wrappers often declare UTF-8 around non-UTF-8 bytes. Validate the
    full source, not just that declaration or a detector's first 10KB. CP932 is a
    superset fallback for Windows Shift-JIS extensions. A few truncated archived
    sequences become visible U+FFFD markers, counted in provenance; never ignore
    bytes or convert an entire Japanese page to Latin-1 for one damaged byte.
    """
    source_hash=hashlib.sha256(data).hexdigest()
    def result(text,encoding,reason,invalid=0):
        return Decoded(text,encoding,invalid,reason,source_hash)
    for prefix,encoding in [(codecs.BOM_UTF32_LE,'utf-32'),(codecs.BOM_UTF32_BE,'utf-32'),
                            (codecs.BOM_UTF8,'utf-8-sig'),(codecs.BOM_UTF16_LE,'utf-16'),(codecs.BOM_UTF16_BE,'utf-16')]:
        if data.startswith(prefix):return result(data.decode(encoding),encoding,'byte_order_mark')
    # ISO-2022-JP is seven-bit; decoding it as ASCII/UTF-8 would retain its
    # escape sequences rather than the actual Japanese characters.
    if re.search(rb'\x1b\$(?:@|B|\(D)',data):
        try:return result(data.decode('iso2022_jp'),'iso2022_jp','iso2022_escape')
        except UnicodeDecodeError:pass
    try:return result(data.decode('utf-8'),'utf-8','valid_utf8')
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
        if enough and invalid<=min(64,max(2,high//50)):
            candidates.append((invalid,-kana,encoding,text))
        elif enough and invalid<=high*.2:
            damaged_japanese=True
    if candidates:
        # Prefer strict decoding, then more recovered Japanese. Stable ordering
        # keeps standard Shift-JIS code points when CP932 is unnecessary.
        invalid,_,encoding,text=min(candidates,key=lambda row:row[:2])
        return result(BAD.sub('\ufffd',text),encoding,'japanese_script_evidence',invalid)
    if damaged_japanese:raise UnicodeError('Japanese source exceeds bounded decoding damage; source bytes retained')
    # Legacy HTML labelled Latin-1 commonly contains Windows smart punctuation.
    # Do not turn the previously readable punctuation into C1 control characters.
    if 'iso8859-1' in declared and ('html' in content_type.lower() or re.search(rb'<(?:!doctype|html|body)\b',data[:8192],re.I)):
        declared.insert(declared.index('iso8859-1'),'cp1252')
    for encoding in list(dict.fromkeys(declared+([detected] if detected else [])+['cp1252','iso8859-1'])):
        try:return result(data.decode(encoding),encoding,'legacy_fallback')
        except (UnicodeDecodeError,LookupError):continue
    raise UnicodeError('Could not decode source')
