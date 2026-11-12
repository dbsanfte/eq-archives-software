import codecs
import hashlib

import pytest

from indexer.source_encoding import decode_source,repaired_title,VERSION


@pytest.mark.parametrize('encoding',['utf-8','shift_jis','cp932','euc_jp','iso2022_jp'])
def test_japanese_codecs_preserve_complete_content_without_a_host_rule(encoding):
    text='<html><title>冒険の記録</title><p>こんにちは。ギルドのみんなで蘇生の相談をしましょう。</p></html>'
    result=decode_source(text.encode(encoding))
    assert result.text==text and result.invalid_bytes==0
    assert result.provenance()['source_sha256']==hashlib.sha256(text.encode(encoding)).hexdigest()
    assert result.provenance()['version']==VERSION


@pytest.mark.parametrize('encoding',['utf-8-sig','utf-16','utf-32'])
def test_byte_order_marks_are_not_embedded_as_content(encoding):
    text='日本語とFrançais and English'
    assert decode_source(text.encode(encoding)).text==text


@pytest.mark.parametrize('declaration',['<meta charset="Shift_JIS">',
    '<meta http-equiv="Content-Type" content="text/html; charset=windows-31j">',
    '<?xml version="1.0" encoding="Shift_JIS"?>','<meta charset="utf-8">'])
def test_windows_extension_and_incorrect_archive_utf8_wrapper(declaration):
    text=declaration+'<p>こんにちは。冒険に行きましょう。①番の髙い山です。</p>'
    result=decode_source(text.encode('cp932'),detection={'encoding':'SHIFT_JIS','confidence':.99})
    assert result.text==text and result.encoding=='cp932'


@pytest.mark.parametrize('detection',[{}, {'encoding':None,'confidence':0},
    {'encoding':'Windows-1254','confidence':.24},{'encoding':'ascii','confidence':1}])
def test_truncated_source_retains_japanese_and_exact_damage_count(detection):
    text='<p>こんにちは。ギルドのみんなで冒険に行きましょう。</p>'
    data=text.encode('cp932')+b'\x82..'+text.encode('cp932')
    result=decode_source(data,detection=detection)
    assert result.text==text+'\ufffd..'+text
    assert result.invalid_bytes==1 and result.encoding in ('shift_jis','cp932')


@pytest.mark.parametrize('text,encoding',[
    ('ASCII guild news.','ascii'),('Français déjà vu, été, élève.','cp1252'),
    ('“Guild” – £50 €100.','cp1252'),('Grüße für zwölf Münzen.','iso8859-1'),
    ('こんにちは、世界。','utf-8'),('你好世界！这是一个测试。','utf-8'),('Привет, мир!','utf-8'),
    ('Αυτό είναι κείμενο.','utf-8')])
def test_unaffected_languages_are_not_mistaken_for_japanese(text,encoding):
    assert decode_source(text.encode(encoding)).text==text


def test_declared_other_encoding_and_unknown_aliases_keep_legacy_fallback():
    text='Привет, мир!'
    assert decode_source(text.encode('cp1251'),'text/html; charset=windows-1251').text==text
    assert decode_source(b'\x80 costs','text/html; charset=not-a-codec').text=='€ costs'
    assert decode_source(b'\x81').encoding=='iso8859-1'


def test_short_hinted_japanese_is_not_required_to_fill_a_chunk():
    text='<p>蘇生の相談</p>'
    assert decode_source(text.encode('cp932'),'text/html; charset=Shift_JIS').text==text


@pytest.mark.parametrize('detection',[{}, {'encoding':'EUC-JP','confidence':.99},
                                     {'encoding':'SHIFT_JIS','confidence':.99}])
def test_repeated_western_symbols_are_not_evidence_of_damaged_japanese(detection):
    text='<html><p>Guild discussion '+('¤'*140)+'</p><footer>ezboard® Copyright ©1999-2002</footer></html>'
    raw=text.encode('cp1252')
    result=decode_source(raw,detection=detection)
    assert result.text==text and result.encoding=='cp1252' and result.invalid_bytes==0
    assert result.source_sha256==hashlib.sha256(raw).hexdigest()


def test_repetitive_japanese_with_an_explicit_charset_retains_its_declared_script():
    text='<html><meta charset="EUC-JP"><p>'+('あ'*100)+'</p></html>'
    result=decode_source(text.encode('euc_jp'))
    assert result.text==text and result.encoding=='euc_jp'


def test_western_decoration_cannot_create_spurious_japanese_regional_anchors():
    text='<html><p>Server '+('¤'*12)+'</p><p>Guild '+('¤'*10)+'</p><footer>ezboard® Copyright ©1999-2002</footer></html>'
    result=decode_source(text.encode('cp1252'))
    assert result.text==text and result.encoding=='cp1252' and not result.regions


def test_late_non_ascii_bytes_are_validated_beyond_detector_sample():
    text=' '*12000+'<p>こんにちは。冒険に行きましょう。①番の髙い山です。</p>'
    assert decode_source(text.encode('cp932'),detection={'encoding':'ascii','confidence':1}).text==text


def test_excessively_damaged_japanese_is_visible_instead_of_latin1_gibberish():
    text='こんにちは。みんなで蘇生の相談をしましょう。'*20
    with pytest.raises(UnicodeError,match='bounded decoding damage'):
        decode_source(text.encode('cp932')+b'\x82 '*80,'text/html; charset=Shift_JIS')


def test_damaged_bom_is_not_misrepresented_as_western_text():
    with pytest.raises(UnicodeError):decode_source(codecs.BOM_UTF16_LE+b'\x01')


def test_title_repair_requires_an_exact_source_title_roundtrip():
    expected='ギルド冒険記録';decoded=decode_source((expected+'こんにちは。'*3).encode('cp932'))
    broken=expected.encode('cp932').decode('latin-1')
    assert repaired_title(broken,expected,decoded)==expected
    for previous in ['A manually assigned title',None,expected]:
        assert repaired_title(previous,expected,decoded)==previous
    assert repaired_title(broken,None,decoded)==broken
    assert repaired_title(broken,expected,decode_source(expected.encode('utf-8')))==broken


def test_repaired_html_title_can_cross_a_process_boundary():
    import pickle
    from bs4 import BeautifulSoup
    expected='ギルド冒険記録'
    decoded=decode_source((expected+'こんにちは。'*3).encode('cp932'))
    soup=BeautifulSoup('<title>'+expected+'</title>'+'<div>'*800+'body'+'</div>'*800,'html.parser')
    repaired=repaired_title(expected.encode('cp932').decode('latin-1'),soup.title.string,decoded)
    assert pickle.loads(pickle.dumps(repaired))==expected
    assert type(repaired) is str


def test_latin1_html_label_keeps_windows_smart_punctuation_readable():
    text='<html><meta charset="ISO-8859-1"><p>“Guild” … £50</p></html>'
    assert decode_source(text.encode('cp1252')).text==text


@pytest.mark.parametrize('punctuation',['–','—','•'])
def test_over_budget_japanese_page_recovers_isolated_western_list_punctuation(punctuation):
    japanese='<meta charset="Shift_JIS"><p>こんにちは。ギルドのみんなで冒険に行きましょう。</p>'
    english=''.join(f'<br>{i} {punctuation} Guild guide {i}' for i in range(20))
    raw=japanese.encode('cp932')+english.encode('cp1252')
    result=decode_source(raw)
    assert result.text==japanese+english and result.invalid_bytes==0
    assert result.provenance()['recoveries']==[
        {'encoding':'cp1252','kind':'isolated_ascii_punctuation','bytes':20}]
    assert result.source_sha256==hashlib.sha256(raw).hexdigest()


def test_mixed_recovery_keeps_unknown_bytes_visible_and_old_bounded_decoding_stable():
    japanese='<p>こんにちは。みんなで蘇生の相談をしましょう。</p>'
    raw=japanese.encode('cp932')+b'\x82..'+b'<br>1 \x96 Guild guide'*20
    result=decode_source(raw)
    assert result.text==japanese+'\ufffd..'+'<br>1 – Guild guide'*20
    assert result.invalid_bytes==1 and result.punctuation_bytes==20
    # Already accepted decoding retains its checkpoint behavior.
    previous=decode_source(japanese.encode('cp932')+b' 1 \x96 Guild')
    assert previous.invalid_bytes==1 and 'recoveries' not in previous.provenance()


def test_punctuation_fallback_does_not_guess_at_damaged_japanese_characters():
    japanese='こんにちは。ギルドのみんなで冒険に行きましょう。'*10
    with pytest.raises(UnicodeError,match='bounded decoding damage'):
        decode_source((japanese+'<br>').encode('cp932')+b'\x96 <br>'*40)


def mixed_posts(tail=b''):
    euc='<p>私は寛の大学入学を喜んだ。</p><p>（わたしは ひろし の だいがくにゅうがく を よろこんだ）</p>'
    sjis='<p>コレア/ジャパン ワールドカップサッカーは ことしは かんこくとにほんで かいさいされます。</p>'
    footer='<footer>ezboard® Copyright ©1999-2002</footer>'
    raw=b'<html><title>Language discussion</title><p>\xa5\xf2</p>'+euc.encode('euc_jp')+sjis.encode('cp932')+footer.encode('cp1252')+tail+b'</html>'
    return raw,'<html><title>Language discussion</title><p>ヲ</p>'+euc+sjis+footer


def test_mixed_html_posts_preserve_both_japanese_encodings_and_western_footer():
    raw,expected=mixed_posts()
    decoded=decode_source(raw)
    assert decoded.text==expected+'</html>' and decoded.invalid_bytes==0
    assert decoded.reason=='regional_evidence' and decoded.encoding=='mixed'
    assert decoded.source_sha256==hashlib.sha256(raw).hexdigest()
    assert {p['encoding'] for p in decoded.provenance()['recoveries']}=={'cp1252','euc_jp','shift_jis'}
    for region in decoded.provenance()['regions']:
        assert 0<=region['raw_start']<region['raw_end']<=len(raw)
        assert raw[region['raw_start']:region['raw_end']].decode(region['encoding']) in decoded.text



def test_mixed_html_retains_small_damage_and_rejects_unbounded_damage():
    raw,expected=mixed_posts(b'<p>\x82 </p>')
    decoded=decode_source(raw)
    assert decoded.text==expected+'<p>\ufffd </p></html>' and decoded.invalid_bytes==1
    with pytest.raises(UnicodeError,match='bounded decoding damage'):
        decode_source(mixed_posts(b'<p>'+b'\x82 '*20+b'</p>')[0])


def test_regional_recovery_requires_independent_encoding_evidence():
    from indexer.source_encoding import regional_decode
    assert regional_decode('<html><p>こんにちは。冒険に行きましょう。</p></html>'.encode('cp932'),4,'cp932') is None


@pytest.mark.parametrize('separator',[b'</p><p>',b' ',b'\n'])
def test_locally_encoded_runs_are_recovered_even_when_one_codec_accepts_the_entire_file(separator):
    euc='こんにちはみんなで冒険';sjis='ギルドのみんなで蘇生の相談をしましょう。'
    raw=b'<html><p>'+euc.encode('euc_jp')+separator+sjis.encode('cp932')+b'</p></html>'
    raw.decode('cp932')  # Codec success alone previously hid the mixed bytes.
    decoded=decode_source(raw)
    assert decoded.text=='<html><p>'+euc+separator.decode()+sjis+'</p></html>'
    assert decoded.encoding=='mixed' and decoded.invalid_bytes==0


@pytest.mark.parametrize('wrapper',[b'',codecs.BOM_UTF8])
def test_utf8_islands_and_western_text_decode_locally_with_exact_original_offsets(wrapper):
    unicode='こんにちは世界。';western='A café guide with “quotes”.'
    raw=wrapper+b'<html><p>'+unicode.encode('utf-8')+b'</p><p>'+western.encode('cp1252')+b'</p></html>'
    decoded=decode_source(raw)
    assert decoded.text=='<html><p>'+unicode+'</p><p>'+western+'</p></html>'
    assert decoded.source_sha256==hashlib.sha256(raw).hexdigest()
    for region in decoded.regions:
        assert region['raw_start']>=len(wrapper)
        assert raw[region['raw_start']:region['raw_end']].decode(region['encoding']) in decoded.text


@pytest.mark.parametrize('encoding,text',[
    ('cp1251','Привет миром'),('iso8859_7','Καλημέρα κόσμε'),('gb18030','你好世界新的冒险')])
def test_declared_legacy_script_can_coexist_with_unicode_posts(encoding,text):
    header=f'<html><meta charset="{encoding}"><p>'
    unicode='Unicode: こんにちは世界。'
    raw=header.encode()+text.encode(encoding)+b'</p><p>'+unicode.encode('utf-8')+b'</p></html>'
    assert decode_source(raw).text==header+text+'</p><p>'+unicode+'</p></html>'


def test_plain_text_lines_and_iso2022_posts_keep_their_local_encodings():
    first='こんにちは世界。';second='ギルドのみんなで蘇生の相談をしましょう。'
    raw=first.encode('iso2022_jp')+b'\n'+second.encode('utf-8')
    assert decode_source(raw).text==first+'\n'+second


def test_regional_metadata_preserves_unicode_attributes_and_exact_title_repairs():
    title='ギルド冒険記録';footer='A café guide with “quotes”.'
    raw=('<html><title>'+title+'</title><img alt="日本語の画像">').encode('utf-8')+footer.encode('cp1252')+b'</html>'
    decoded=decode_source(raw)
    assert decoded.text=='<html><title>'+title+'</title><img alt="日本語の画像">'+footer+'</html>'
    assert repaired_title(title.encode('utf-8').decode('latin1'),title,decoded)==title
    assert repaired_title('Operator title',title,decoded)=='Operator title'


def test_adjacent_compatible_regions_are_coalesced_without_losing_original_byte_coverage():
    unicode='こんにちは世界。';western='café '*100
    raw=unicode.encode('utf-8')+b'\n'+western.encode('cp1252')
    decoded=decode_source(raw)
    assert decoded.text==unicode+'\n'+western
    assert len(decoded.regions)==2
    assert raw[decoded.regions[1]['raw_start']:decoded.regions[1]['raw_end']].decode('cp1252') in western


@pytest.mark.parametrize('other_encoding',['utf-8','cp932','euc_jp'])
def test_html_escaped_stateful_bytes_are_recovered_before_decoding_each_post(other_encoding):
    import html
    japanese='ありがとう。あの人は日本語を学びたい。'
    other='こんにちは世界。ギルドのみんなで冒険に行きましょう。'
    escaped=html.escape(japanese.encode('iso2022_jp').decode('ascii'),quote=True).encode('ascii')
    raw=b'<html><p>&quot;Outside quote&quot;</p><p>'+escaped+b'</p><p>'+other.encode(other_encoding)+b'</p></html>'
    decoded=decode_source(raw)
    assert decoded.text=='<html><p>&quot;Outside quote&quot;</p><p>'+japanese+'</p><p>'+other+'</p></html>'
    assert '\x1b' not in decoded.text and decoded.invalid_bytes==0
    assert decoded.restored_entities>0
    assert decoded.source_sha256==hashlib.sha256(raw).hexdigest()
    assert any(p['kind']=='escaped_jis_bytes' for p in decoded.provenance()['recoveries'])


def test_escaped_iso2022_only_and_truncation_keep_entity_recovery_and_damage_separate():
    import html
    japanese='ありがとう。あの人は日本語を学びたい。'
    escaped=html.escape(japanese.encode('iso2022_jp').decode('ascii'),quote=True).encode('ascii')
    raw=b'<html><p>'+escaped+b'\x1b$B$</p></html>'
    decoded=decode_source(raw)
    assert japanese in decoded.text and decoded.text.count('\ufffd')==1
    assert decoded.invalid_bytes==1 and decoded.restored_entities>0 and '\x1b' not in decoded.text
    with pytest.raises(UnicodeError,match='bounded decoding damage'):
        decode_source(b'<html>'+escaped+b'\x1b$B$\x1b(B'*10+b'</html>')


def test_concurrent_stateful_decodes_keep_each_sources_damage_counts_isolated():
    from concurrent.futures import ThreadPoolExecutor
    japanese='こんにちは世界。'
    def read(i):
        data=japanese.encode('iso2022_jp')+(b'\x1b$B$' if i%2 else b'')
        result=decode_source(data)
        return result.invalid_bytes,result.text
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(read,range(20)))
    assert results==[(i%2,japanese+('\ufffd' if i%2 else '')) for i in range(20)]


@pytest.mark.parametrize('wrapper',[
    b'<script><!-- %s --></script>',b'<style>/* %s */</style>',b'<!-- %s -->'])
def test_hidden_generator_comments_cannot_establish_the_readers_language(wrapper):
    # Old page generators paste partially escaped Japanese comments into an
    # otherwise Western page. Damage there must not reject its readable body.
    comment='ステータス行に文字列を表示します'.encode('cp932')+b'\x82 '*4
    raw=b'<html><head>'+wrapper%comment+b'</head><body><p>Guild caf\xe9 guide. Copyright \xa9 2002.</p></body></html>'
    result=decode_source(raw,detection={'encoding':'SHIFT_JIS','confidence':.99})
    assert result.text==raw.decode('iso8859-1') and result.invalid_bytes==0
    assert result.source_sha256==hashlib.sha256(raw).hexdigest()


def test_visible_japanese_evidence_remains_valid_alongside_hidden_generator_code():
    body='こんにちは。ギルドのみんなで冒険に行きましょう。'
    text='<html><script>// hidden status text</script><p>'+body+'</p></html>'
    assert decode_source(text.encode('cp932')).text==text


@pytest.mark.parametrize('body',['｢冒険の記録｣','半角ｶﾅと全角の日本語を保存する。'])
def test_iso2022_extended_kana_is_valid_content_without_damage_or_host_rules(body):
    raw=('<html><meta charset="iso-2022-jp"><p>'+body+'</p></html>').encode('iso2022_jp_ext')
    decoded=decode_source(raw)
    assert decoded.text=='<html><meta charset="iso-2022-jp"><p>'+body+'</p></html>'
    assert decoded.invalid_bytes==0 and decoded.source_sha256==hashlib.sha256(raw).hexdigest()
    assert any(r['kind']=='jis_katakana' for r in decoded.provenance()['recoveries'])


def test_pure_jis_halfwidth_designation_is_not_mistaken_for_ascii():
    text='｢ｶﾅ｣';decoded=decode_source(text.encode('iso2022_jp_ext'))
    assert decoded.text==text and decoded.invalid_bytes==0


@pytest.mark.parametrize('pair,expected',[(b'-!','①'),(b'-"','②'),(b'-6','Ⅱ')])
def test_vendor_jis_rows_recover_assigned_windows_characters_only_inside_jis(pair,expected):
    raw=b'<p>\x1b$B'+pair+b'\x1b(B Guild history</p>'
    decoded=decode_source(raw)
    assert decoded.text=='<p>'+expected+' Guild history</p>' and decoded.invalid_bytes==0
    assert any(r['kind']=='jis_vendor_extension' and r['characters']==1 for r in decoded.provenance()['recoveries'])
    assert decode_source(b'<p>'+pair+b'</p>').text=='<p>'+pair.decode()+'</p>'


def test_western_words_and_copyright_stay_western_outside_short_jis_glyphs():
    raw=b'<html><title>Guild fell\xe1</title><p>I don\xb4t agree. Copyright \xa91999.</p><p>\x1b$B!&amp;\x1b(B</p></html>'
    decoded=decode_source(raw)
    assert decoded.text=='<html><title>Guild fellá</title><p>I don´t agree. Copyright ©1999.</p><p>・</p></html>'
    assert decoded.invalid_bytes==0 and decoded.restored_entities==1
    assert sum(r.get('bytes',0) for r in decoded.stateful_recoveries if r['kind']=='western_outside_jis')==3


def test_unassigned_jis_and_unsupported_controls_are_not_turned_into_western_text():
    japanese='こんにちは世界。'.encode('iso2022_jp')
    with pytest.raises(UnicodeError,match='bounded decoding damage'):
        decode_source(japanese+b'\x1b$B~\x7e\x1b(B'*20)
    decoded=decode_source(japanese+b'\x81')
    assert decoded.invalid_bytes==1 and decoded.text.endswith('\ufffd')


def test_iso2022_title_repair_requires_exact_extended_source_proof():
    title='｢冒険の記録｣';decoded=decode_source(title.encode('iso2022_jp_ext'))
    assert repaired_title(title.encode('iso2022_jp_ext').decode('latin1'),title,decoded)==title
    assert repaired_title('Operator chosen title',title,decoded)=='Operator chosen title'
