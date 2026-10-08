"""Archived entry-point redirects retain exact sources and bounded site scope."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from acquisition import sample
from common import CrawlError, Store, digest

ROOT='http://www.solusekro.com/'
TARGET=ROOT+'eq/'
STAMP='20000118215840'
FINAL='20001218014500'


class RedirectSamples(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name));self.addCleanup(self.store.close)
        self.store.db.execute("INSERT INTO candidates(id,url,scope,coverage,evidence) VALUES ('site',?,?,?,?)", (ROOT,ROOT,'{}','[]'))
        self.store.db.commit()
        self.args=SimpleNamespace(max_candidates=1,samples_per_candidate=2,retry_unresolved=True)
        self.calls=[];self.target=TARGET;self.stamp=FINAL;self.fail_capture=False

    def call(self,job):
        self.calls.append(job)
        if job['op']=='list':
            base={'captures':[],'listing_limited':False,'available_rows':0,'identity_variants':[]}
            if job['url']==ROOT:
                return {**base,'redirects':[{'url':ROOT,'timestamp':STAMP,'statuscode':'302','digest':'R','length':'20'}],
                        'response_types':[['302','text/html']]}
            return {**base,'captures':[{'url':self.target,'timestamp':self.stamp,'digest':'D','length':'50'}]}
        if job['op']=='resolve':
            return {'url':self.target,'timestamp':self.stamp,'requested_url':ROOT,'requested_timestamp':STAMP,
                    'timestamp_basis':'memento_datetime','redirects':[{'from':'/web/'+STAMP+'id_/'+ROOT,
                     'to':'/web/'+self.stamp+'id_/'+self.target,'status':302}]}
        if self.fail_capture:raise CrawlError('Wayback connection failed after bounded retries')
        raw=b'<title>Welcome to Solusek Ro</title><p>EverQuest server news and guilds.</p>'
        Path(job['destination']).write_bytes(raw)
        return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}

    def check(self):
        sample(self.args,self.store,downloader=self)
        row=dict(self.store.candidates()[0]);row['captures']=json.loads(row['captures'])
        return row

    def test_redirect_recovers_actual_source_without_rewriting_candidate_or_scope(self):
        row=self.check()
        self.assertEqual(row['state'],'sampled')
        self.assertEqual((row['url'],row['scope']),(ROOT,ROOT))
        capture=row['captures'][0]
        self.assertEqual((capture['url'],capture['timestamp']),(TARGET,FINAL))
        self.assertEqual(capture['entry_redirect']['requested_url'],ROOT)
        self.assertEqual(capture['entry_redirect']['requested_timestamp'],STAMP)
        self.assertTrue(all(job['to']=='20011231235959' for job in self.calls))
        self.assertEqual([job['op'] for job in self.calls],['list','resolve','list','capture'])

    def test_redirect_resolution_is_cached_across_transport_retry(self):
        self.fail_capture=True
        self.assertEqual(self.check()['state'],'sample_error')
        self.fail_capture=False
        self.assertEqual(self.check()['state'],'sampled')
        self.assertEqual(sum(job['op']=='resolve' for job in self.calls),1)

    def test_verified_http_www_alias_redirect_preserves_actual_identity(self):
        self.target='https://solusekro.com/eq/'
        row=self.check()
        self.assertEqual(row['state'],'sampled')
        self.assertEqual(row['captures'][0]['url'],self.target)
        self.assertEqual(row['url'],ROOT)

    def test_redirect_never_expands_a_page_only_scope_or_changes_host(self):
        for target,coverage in [(TARGET,{'scope_mode':'page'}),('http://other.example/',{})]:
            with self.subTest(target=target):
                self.target=target;self.calls=[]
                self.store.db.execute("UPDATE candidates SET coverage=?,state='discovered',captures='[]'",(json.dumps(coverage),))
                self.store.db.commit()
                row=self.check()
                self.assertEqual(row['state'],'sample_error')
                self.assertIn('outside the saved capture scope',row['error'])
                self.assertEqual(row['captures'],[])
                self.assertFalse(any(job['op']=='capture' for job in self.calls))

    def test_destination_outside_the_tier_is_rejected_before_sampling(self):
        self.stamp='20080101000000'
        row=self.check()
        self.assertEqual(row['captures'],[])
        self.assertIn('date',row['error'])

    def test_redirect_chain_cannot_cross_a_shared_host_account(self):
        root='http://geocities.com/eqguild/'
        target='http://geocities.com/another-account/'
        self.store.db.execute('UPDATE candidates SET url=?,scope=?',(root,root));self.store.db.commit()
        def acquire(job):
            self.calls.append(job)
            if job['op']=='list':
                return {'captures':[],'listing_limited':False,'available_rows':0,'identity_variants':[],
                        'redirects':[{'url':root,'timestamp':STAMP,'digest':'R'}]}
            self.assertEqual(job['op'],'resolve')
            return {'url':target,'timestamp':FINAL,'requested_url':root,'requested_timestamp':STAMP,
                    'redirects':[{'from':'/web/'+STAMP+'id_/'+root,'to':'/web/'+FINAL+'id_/'+target,'status':302}]}
        self.call=acquire
        row=self.check()
        self.assertEqual(row['captures'],[])
        self.assertIn('outside the saved capture scope',row['error'])
        self.assertEqual([job['op'] for job in self.calls],['list','resolve'])

    def test_archived_errors_are_not_reported_as_no_captures(self):
        def listing(job):
            return {'captures':[],'listing_limited':False,'available_rows':0,'identity_variants':[],
                    'redirects':[],'response_types':[['403','text/html'],['500','text/html']]}
        self.call=listing
        row=self.check()
        self.assertEqual(row['state'],'unavailable')
        self.assertIn('archived responses',row['error'])
        self.assertNotIn('No exact HTML captures',row['error'])


if __name__=='__main__':unittest.main()
