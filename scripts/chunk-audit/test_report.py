import copy
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('chunk_report',Path(__file__).with_name('build-report.py'))
report=importlib.util.module_from_spec(spec);spec.loader.exec_module(report)


class ReportTest(unittest.TestCase):
    def row(self,index=0):
        return {'id':f'websites/example.org/20000101000000/page{index}.html','source_sha256':'a'*64,
                'method':'html_structure','view':'Complete source.','passages':[{'text':'Complete source.','start':0,'end':16,
                'title':'Title','context':'','kind':'prose','tokens':20}]}

    def test_complete_view_is_required_and_source_change_is_rejected(self):
        row=self.row();self.assertTrue(report.verified(row))
        for change in ({'end':15},{'tokens':481},{'start':1},{'text':'Changed source.'}):
            bad=copy.deepcopy(row);bad['passages'][0].update(change)
            with self.assertRaises(ValueError):report.verified(bad)
        bad=copy.deepcopy(row);bad['view']+=' Missing tail'
        with self.assertRaises(ValueError):report.verified(bad)

    def test_compare_only_same_id_and_source_and_do_not_export_full_views(self):
        rows=[self.row(i) for i in range(4)];template={'sites':[{'site':'example.org','pages':4,'policies':['Generic']} ]}
        after=copy.deepcopy(rows);after[0]['passages'][0]['context']='Source identity\n'
        result=report.build(template,{'example.org':rows},{'example.org':after})['sites'][0]
        self.assertEqual((result['verified'],result['changed'],result['status']),(4,1,'fixed'))
        self.assertNotIn('view',result);self.assertNotIn('passages',result)
        after[0]['source_sha256']='b'*64
        with self.assertRaises(ValueError):report.build(template,{'example.org':rows},{'example.org':after})
        with self.assertRaises(ValueError):report.build(template,{'example.org':rows},{'example.org':after[:3]})

    def test_evidence_is_a_dated_encoded_wayback_url_and_previews_are_bounded(self):
        row=self.row();row['id']='websites/example.org/20000101000000/a <script>?q=1&x=2'
        link=report.evidence(row)
        self.assertIn('/20000101000000/http://example.org/a%20%3Cscript%3E?q=1&x=2',link['url'])
        row['passages'][0]['context']='x'*5000
        self.assertLessEqual(len(report.preview(row)),700)
        row['id']='not-a-capture'
        with self.assertRaises(ValueError):report.evidence(row)


if __name__=='__main__':unittest.main()
