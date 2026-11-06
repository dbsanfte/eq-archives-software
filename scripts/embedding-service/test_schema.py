import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

spec=importlib.util.spec_from_file_location('embedding_schema',Path(__file__).with_name('provision-schema.py'))
schema=importlib.util.module_from_spec(spec);spec.loader.exec_module(schema)


class SchemaTests(unittest.TestCase):
    def test_additive_schema_is_installed_before_visual_queries_without_writing_documents(self):
        secret={'data':{'ELASTIC_PASSWORD':base64.b64encode(b'fixture-private-password').decode()}}
        def respond(request,timeout):
            self.assertEqual(request.method,'PUT')
            self.assertEqual(request.full_url,'http://fixture:9200/eq-archive/_mapping')
            self.assertEqual(timeout,30)
            mapping=json.loads(request.data)['properties']
            self.assertEqual(mapping['image_vector']['dims'],768)
            self.assertEqual(mapping['image_vector']['type'],'dense_vector')
            self.assertFalse(mapping['text_passage_provenance']['enabled'])
            self.assertFalse(mapping['text']['properties']['context']['index'])
            return io.StringIO('{"acknowledged":true}')
        output=io.StringIO()
        with patch('sys.stdin',io.StringIO(json.dumps(secret))),patch.dict('os.environ',{'EMBEDDING_SCHEMA_ES_URL':'http://fixture:9200'}),patch.object(schema.urllib.request,'urlopen',respond),redirect_stdout(output):
            schema.main()
        self.assertNotIn('fixture-private-password',output.getvalue())

    def test_failed_mapping_does_not_leak_upstream_or_credential_details(self):
        secret={'data':{'ELASTIC_PASSWORD':base64.b64encode(b'fixture-key').decode()}}
        for failure in (OSError('private upstream'),ValueError('private payload')):
            with patch('sys.stdin',io.StringIO(json.dumps(secret))),patch.dict('os.environ',{'EMBEDDING_SCHEMA_ES_URL':'http://fixture'}),patch.object(schema.urllib.request,'urlopen',side_effect=failure),self.assertRaises(SystemExit) as caught:
                schema.main()
            self.assertNotIn('private',str(caught.exception))

    def test_verified_discussion_mapping_is_additive_and_keeps_proof_out_of_search_text(self):
        properties=json.loads(Path(__file__).with_name('schema.json').read_text())['properties']['discussion']['properties']
        self.assertEqual(properties['key']['type'],'keyword')
        self.assertEqual(properties['version']['type'],'keyword')
        for name in ('title','source_sha256','evidence_sha256','evidence_id'):
            self.assertFalse(properties[name]['index'])
