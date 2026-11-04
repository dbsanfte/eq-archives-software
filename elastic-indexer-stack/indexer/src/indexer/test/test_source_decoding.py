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


def test_latin1_html_label_keeps_windows_smart_punctuation_readable():
    text='<html><meta charset="ISO-8859-1"><p>“Guild” … £50</p></html>'
    assert decode_source(text.encode('cp1252')).text==text
