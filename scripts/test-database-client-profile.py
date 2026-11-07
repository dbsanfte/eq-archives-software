"""Database TLS transport regressions; no production addresses/credentials used."""
import copy
import importlib.util
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

spec=importlib.util.spec_from_file_location('profile',Path(__file__).with_name('database-client-profile.py'))
profile=importlib.util.module_from_spec(spec);spec.loader.exec_module(profile)
ROOTS=Path('/etc/ssl/certs/ca-certificates.crt').read_text()


def document(scheme='https'):
    return {'data':{'url':scheme+'://elasticsearch.eqarchives-es.svc.cluster.local:9200',
                    'host':scheme+'://elasticsearch.eqarchives-es.svc.cluster.local','port':'9200','ca-bundle.pem':ROOTS}}


class DatabaseProfile(unittest.TestCase):
    def test_existing_https_is_preserved_without_mutation(self):
        kube=Mock(return_value=subprocess.CompletedProcess([],0,json.dumps(document()),''))
        self.assertEqual(profile.load(kube),document()['data']);self.assertEqual(kube.call_count,1)
    def test_missing_profile_is_created_once_with_public_roots(self):
        kube=Mock(side_effect=[subprocess.CompletedProcess([],0,'',''),subprocess.CompletedProcess([],0,'created','')])
        data=profile.load(kube,ROOTS)
        self.assertEqual(data,document('http')['data'])
        created=json.loads(kube.call_args.kwargs['input'])
        self.assertEqual(created['metadata']['name'],profile.NAME)
        self.assertIn('create',kube.call_args.args[0]);self.assertNotIn('apply',kube.call_args.args[0])
    def test_concurrent_https_creation_wins_over_http_bootstrap(self):
        kube=Mock(side_effect=[subprocess.CompletedProcess([],0,'',''),subprocess.CompletedProcess([],1,'','exists'),subprocess.CompletedProcess([],0,json.dumps(document()),'')])
        self.assertEqual(profile.load(kube,ROOTS),document()['data'])
    def test_bad_profile_fails_instead_of_disabling_verification(self):
        for url in ('http://evil:9200','https://elasticsearch.eqarchives-es.svc.cluster.local:9200/x','https://user:password@elasticsearch.eqarchives-es.svc.cluster.local:9200','https://elasticsearch.eqarchives-es.svc.cluster.local:9200?x=1'):
            bad=document();bad['data']['url']=url
            with self.subTest(url=url),self.assertRaises(ValueError):profile.validate(bad)
        bad=document();bad['data']['ca-bundle.pem']='invalid'
        with self.assertRaises(ssl.SSLError):profile.validate(bad)
        bad=document();bad['data']['host']='http://elasticsearch.eqarchives-es.svc.cluster.local'
        with self.assertRaises(ValueError):profile.validate(bad)
    def test_exports_verified_trust_to_operator_and_checksum_tracks_changes(self):
        with tempfile.TemporaryDirectory(prefix="database ' profile ") as directory:
            data=document()['data'];env=profile.export(directory,data,'10.43.61.29')
            result=json.loads(subprocess.check_output(['bash','-c','source "$1"; python3 -c \'import os,json; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith("DATABASE_") or k in ("SSL_CERT_FILE","CURL_CA_BUNDLE","REQUESTS_CA_BUNDLE")}))\'','bash',str(env)],text=True))
            self.assertEqual(result['DATABASE_OPERATOR_URL'],'https://10.43.61.29:9200')
            self.assertEqual(Path(result['SSL_CERT_FILE']).read_text(),ROOTS)
            self.assertEqual(result['SSL_CERT_FILE'],result['CURL_CA_BUNDLE'])
            self.assertEqual(env.stat().st_mode&0o777,0o600)
            old=result['DATABASE_CLIENT_CHECKSUM'];data=copy.deepcopy(data);data['ca-bundle.pem']+='\n'
            text=profile.export(directory,data,'10.43.61.29').read_text()
            self.assertNotIn(old,text)
    def test_invalid_operator_ip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory,self.assertRaises(ValueError):profile.export(directory,document()['data'],'localhost;echo bad')


if __name__=='__main__':unittest.main()
