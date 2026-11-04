"""Byte-level regressions for archival Japanese and unaffected encodings."""
import pytest

from indexer.archive_handler import ArchiveHandler
from indexer.text_handler import TextHandler


@pytest.mark.parametrize('declared',['utf-8','Shift_JIS',''])
def test_windows_japanese_extension_survives_in_reader_and_source_html(tmp_path,declared):
    html=f'<html><head><meta charset="{declared}"><title>ギルド冒険記録</title></head><body><p>こんにちは。蘇生の相談、①番の剣と髙い山。</p></body></html>'
    path=tmp_path/'page.html';path.write_bytes(html.encode('cp932'))
    reader=TextHandler(ArchiveHandler(),None,llm_enrichment_enabled=False)
    document=reader._get_documents_from_file('websites/any.example/20000101000000/index.html',str(path))[0]
    assert 'こんにちは。蘇生の相談、①番の剣と髙い山。' in document.page_content
    assert 'こんにちは。蘇生の相談、①番の剣と髙い山。' in document.metadata['source_html']
    assert '\ufffd' not in document.page_content


def test_truncated_japanese_byte_is_local_damage_not_a_latin1_page(tmp_path):
    paragraph='蘇生の相談です。ギルドのみんなで冒険しましょう。'
    raw=('<html><meta charset="Shift_JIS"><p>'+paragraph).encode('cp932')+b'\x82..</p><p>'+paragraph.encode('cp932')+b'</p></html>'
    path=tmp_path/'page.html';path.write_bytes(raw)
    reader=TextHandler(ArchiveHandler(),None,llm_enrichment_enabled=False)
    text,_=reader._detect_and_read_file(str(path))
    assert text.count(paragraph)==2
    assert '\ufffd..' in text
    assert '\x82' not in text
