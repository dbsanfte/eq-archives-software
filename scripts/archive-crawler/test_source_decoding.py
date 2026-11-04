import unittest

from common import decode,Page


class SourceDecodingTests(unittest.TestCase):
    def test_windows_japanese_is_readable_in_standalone_discovery(self):
        text='<html><meta charset="utf-8"><title>ギルド案内</title><p>こんにちは。①番の髙い山に行きましょう。</p></html>'
        body,encoding=decode(text.encode('cp932'))
        self.assertEqual(body,text)
        self.assertEqual(encoding,'cp932')
        page=Page('http://independent.example/');page.feed(body)
        self.assertIn('ギルド案内',' '.join(page.title))

    def test_truncated_japanese_keeps_visible_damage_instead_of_gibberish(self):
        text='<p>こんにちは。ギルドのみんなで蘇生の相談をしましょう。</p>'
        body,encoding=decode(text.encode('cp932')+b'\x82..','text/html; charset=Shift_JIS')
        self.assertEqual(body,text+'\ufffd..')

    def test_other_encodings_keep_their_content(self):
        self.assertEqual(decode('Français £50'.encode('cp1252'))[0],'Français £50')
        self.assertEqual(decode('你好世界'.encode('utf-8'))[0],'你好世界')


if __name__=='__main__':unittest.main()
