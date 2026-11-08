import codecs

import pytest

from source_assets import binary_source


@pytest.mark.parametrize('raw', [
    b'(DWF V00.30)\x00\x00\x00\x00', b'PK\x03\x04archive', b'PK\x05\x06empty',
    b'\x1f\x8bgzip', b'%PDF-1.7 document', b'\x7fELF', b'\x89PNG\r\n\x1a\n',
    b'\xff\xd8\xffimage', b'GIF87a', b'GIF89a', b'unknown\0\1\2\3\4\5\6\0' * 20,
])
def test_binary_evidence_overrides_unreliable_server_text_mime(raw):
    assert binary_source(raw)


@pytest.mark.parametrize('raw', [
    b'<html><p>EverQuest guide</p></html>', b'A normal plain text map legend.',
    'エバークエスト攻略情報'.encode('cp932'), 'エバークエスト攻略情報'.encode('iso2022_jp'),
    'EverQuest 日本語'.encode('utf-16'), 'EverQuest 日本語'.encode('utf-32'),
    codecs.BOM_UTF8 + b'EverQuest', b'An isolated\0damaged character.', b'\x1b[31mRed text\x1b[0m',
    b'',
])
def test_unicode_legacy_japanese_and_uncertain_text_keep_full_reader_path(raw):
    assert not binary_source(raw)
