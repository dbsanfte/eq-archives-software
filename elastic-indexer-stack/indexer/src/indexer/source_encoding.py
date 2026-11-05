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
    punctuation_bytes:int=0
    segment_encodings:tuple=()

    def provenance(self):
        value={'version':VERSION,'encoding':self.encoding,'invalid_bytes':self.invalid_bytes,
               'reason':self.reason,'source_sha256':self.source_sha256}
        if self.punctuation_bytes:
            value['recoveries']=[{'encoding':'cp1252','kind':'isolated_ascii_punctuation',
                                  'bytes':self.punctuation_bytes}]
        if self.segment_encodings:
            value.setdefault('recoveries',[]).extend(
                {'encoding':encoding,'kind':'html_text_segments','segments':count}
                for encoding,count in self.segment_encodings)
        return value


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


def mixed_html(data,limit):
    """Recover independently encoded HTML text only with two strong scripts.

    Some old boards preserved each poster's bytes. Require strict, substantial
    kana evidence for both EUC-JP and Shift-JIS before considering short labels;
    this is never a fallback for a merely damaged single-encoding document.
    """
    if not re.search(rb'<(?:!doctype|html|body)\b',data[:8192],re.I):return None
    parts=re.split(rb'(<[^>]*>)',data);selected={};families=set()
    def choices(raw):
        found=[]
        for encoding in ('shift_jis','cp932','euc_jp'):
            try:text=raw.decode(encoding)
            except UnicodeDecodeError:continue
            kana=len(KANA.findall(text));kanji=len(KANJI.findall(text));half=len(HALF_KANA.findall(text))
            if kana and kana*4>=kana+kanji+half:found.append((kana,kanji,encoding,text))
        return found
    for i,raw in enumerate(parts):
        if i%2 or raw.isascii():continue
        candidates=[c for c in choices(raw) if c[0]>=2 and c[0]+c[1]>=4]
        if candidates:
            best=max(candidates,key=lambda c:c[:2]);selected[i]=best
            families.add('shift_jis' if best[2]=='cp932' else best[2])
    if families!={'shift_jis','euc_jp'}:return None
    output=[];counts={};invalid=0
    for i,raw in enumerate(parts):
        if raw.isascii():output.append(raw.decode('ascii'));continue
        best=selected.get(i)
        if best is None and not i%2:
            candidates=choices(raw)
            if candidates:best=max(candidates,key=lambda c:c[:2])
        if best:
            encoding,text=best[2:]
        elif all(b<128 or b>=160 for b in raw) and re.search(rb'[A-Za-z]{4}',raw):
            # Western prose and copyright labels; never reinterpret damaged
            # Japanese lead bytes in the C1 range as smart punctuation.
            encoding='cp1252';text=raw.decode(encoding)
        else:
            encoding='cp932';text=raw.decode(encoding,errors='surrogateescape')
            invalid+=len(BAD.findall(text))
        output.append(BAD.sub('\ufffd',text));counts[encoding]=counts.get(encoding,0)+1
    if invalid>limit:return None
    return ''.join(output),invalid,tuple(sorted(counts.items()))


def decode_source(data,content_type='',*,detection=None):
    """Prefer Unicode, then source-backed legacy encodings over lossless gibberish.

    Old archive wrappers often declare UTF-8 around non-UTF-8 bytes. Validate the
    full source, not just that declaration or a detector's first 10KB. CP932 is a
    superset fallback for Windows Shift-JIS extensions. A few truncated archived
    sequences become visible U+FFFD markers, counted in provenance; never ignore
    bytes or convert an entire Japanese page to Latin-1 for one damaged byte.
    """
    source_hash=hashlib.sha256(data).hexdigest()
    def result(text,encoding,reason,invalid=0,punctuation=0,segments=()):
        return Decoded(text,encoding,invalid,reason,source_hash,punctuation,segments)
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
        return result(BAD.sub('\ufffd',text),encoding,'japanese_script_evidence',invalid,punctuation)
    if damaged_japanese:
        recovered=mixed_html(data,limit)
        if recovered:
            text,invalid,segments=recovered
            return result(text,'mixed','mixed_japanese_html_segments',invalid,segments=segments)
        raise UnicodeError('Japanese source exceeds bounded decoding damage; source bytes retained')
    # Legacy HTML labelled Latin-1 commonly contains Windows smart punctuation.
    # Do not turn the previously readable punctuation into C1 control characters.
    if 'iso8859-1' in declared and ('html' in content_type.lower() or re.search(rb'<(?:!doctype|html|body)\b',data[:8192],re.I)):
        declared.insert(declared.index('iso8859-1'),'cp1252')
    for encoding in list(dict.fromkeys(declared+([detected] if detected else [])+['cp1252','iso8859-1'])):
        try:return result(data.decode(encoding),encoding,'legacy_fallback')
        except (UnicodeDecodeError,LookupError):continue
    raise UnicodeError('Could not decode source')
