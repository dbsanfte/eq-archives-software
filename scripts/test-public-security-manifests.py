#!/usr/bin/env python3
"""Regression checks for the public workload's credential and network boundaries."""
from pathlib import Path
from contextlib import redirect_stdout
import io
import runpy
import subprocess
import unittest
from unittest.mock import patch
import yaml
ROOT=Path(__file__).resolve().parents[1]
DIR=ROOT/'elastic-indexer-stack/k8s-manifests'


class PublicSecurity(unittest.TestCase):
    def setUp(self):
        self.resources=[r for name in ('01-frontend.yaml','mcp.yaml','public-network-policy.yaml','elasticsearch-service.yaml') for r in yaml.safe_load_all((DIR/name).read_text()) if r]
    def get(self,kind,name):
        return next(r for r in self.resources if r['kind']==kind and r['metadata']['name']==name)
    def test_frontend_has_no_backend_secrets_or_direct_database_endpoint(self):
        pod=self.get('Deployment','search-eqarchives')['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertTrue(pod['securityContext']['runAsNonRoot'])
        self.assertEqual(pod['securityContext']['fsGroup'],10001)
        self.assertEqual(pod['securityContext']['seccompProfile']['type'],'RuntimeDefault')
        container=pod['containers'][0]
        self.assertEqual(container['ports'][0]['containerPort'],8080)
        self.assertEqual(container['securityContext'],{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}})
        self.assertNotIn('secret',str(pod['volumes']).lower())
        self.assertEqual({e['name'] for e in container['env']},{'LOG_LEVEL','SEARCH_API_URL','ELASTICSEARCH_INDEX'})
        self.assertNotIn('elasticsearch.eqarchives',str(container['env']))
    def test_database_client_uses_persistent_endpoint_and_verified_trust(self):
        pod=self.get('Deployment','eqarchives-mcp')['spec']['template']['spec']
        env={e['name']:e for e in pod['containers'][0]['env']}
        self.assertEqual(env['ELASTICSEARCH_URL']['valueFrom']['configMapKeyRef'],{'name':'eqarchives-database-client','key':'url'})
        mount=next(m for m in pod['containers'][0]['volumeMounts'] if m['name']=='database-trust')
        self.assertEqual(mount['mountPath'],'/usr/local/lib/python3.13/site-packages/certifi/cacert.pem')
        self.assertTrue(mount['readOnly'])
        self.assertEqual(mount['subPath'],'ca-bundle.pem')
        self.assertEqual(next(v for v in pod['volumes'] if v['name']=='database-trust')['configMap']['name'],'eqarchives-database-client')
    def test_frontend_network_can_only_reach_dns_and_the_bounded_api(self):
        p=self.get('NetworkPolicy','public-static-frontend')['spec']
        self.assertEqual(p['podSelector'],{'matchLabels':{'app':'search-eqarchives'}})
        self.assertEqual(p['policyTypes'],['Ingress','Egress'])
        self.assertEqual(len(p['egress']),2)
        self.assertEqual(p['egress'][1],{'to':[{'podSelector':{'matchLabels':{'app':'eqarchives-mcp'}}}], 'ports':[{'protocol':'TCP','port':8080}]})
        self.assertEqual(p['ingress'][0]['from'][0]['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'],'kube-system')
    def test_api_cannot_reach_control_plane_lan_or_external_urls(self):
        p=self.get('NetworkPolicy','public-search-api')['spec']
        self.assertEqual(len(p['egress']),3)
        for rule in p['egress']:
            self.assertNotIn('ipBlock',str(rule))
            self.assertTrue(rule['ports'])
        self.assertEqual(p['egress'][1]['to'],[{'podSelector':{'matchLabels':{'app':'elasticsearch'}}}])
        self.assertEqual(p['egress'][2]['to'],[{'podSelector':{'matchLabels':{'app':'nomic-embeddings'}}}])
    def test_database_keeps_job_access_without_allowing_the_static_frontend(self):
        p=self.get('NetworkPolicy','archive-database-ingress')['spec']
        self.assertEqual(p['policyTypes'],['Ingress'])
        self.assertEqual(p['podSelector'],{'matchLabels':{'app':'elasticsearch'}})
        self.assertIn({'podSelector':{'matchExpressions':[{'key':'job-name','operator':'Exists'}]}},p['ingress'][0]['from'])
        self.assertNotIn('search-eqarchives',str(p))
    def test_database_service_does_not_expose_nodeports_or_transport(self):
        service=self.get('Service','elasticsearch')['spec']
        self.assertEqual(service['type'],'ClusterIP')
        self.assertEqual(service['ports'],[{'name':'rest','port':9200,'targetPort':9200,'protocol':'TCP'}])
        self.assertEqual(service['selector'],{'app':'elasticsearch'})
    def test_security_headers_are_inherited_even_on_cacheable_assets(self):
        template=(ROOT/'elastic-indexer-stack/frontend/nginx.conf.template').read_text()
        self.assertEqual(template.count('add_header Cache-Control'),3)
        self.assertEqual(template.count('include /etc/nginx/security-headers.conf;'),4)
        self.assertNotIn('ELASTICSEARCH_AUTHORIZATION',template)
        self.assertNotIn('OPENAI_API_KEY',template)
        self.assertIn('location = /elasticsearch/',template)
    def test_public_ingress_has_a_shared_concurrency_ceiling(self):
        self.assertEqual(self.get('Middleware','public-global-inflight')['spec'],{'inFlightReq':{'amount':64}})
        route=self.get('Ingress','search-eqarchives-root')
        self.assertIn('eqarchives-es-public-global-inflight@kubernetescrd',route['metadata']['annotations']['traefik.ingress.kubernetes.io/router.middlewares'])
    def test_online_routing_validator_accepts_adapter_and_rejects_cuda_for_search(self):
        rendered=subprocess.check_output(['kubectl','kustomize',str(DIR)],text=True)
        checker=ROOT/'scripts/embedding-service/check-manifests.py'
        with patch('sys.stdin',io.StringIO(rendered)),redirect_stdout(io.StringIO()):
            runpy.run_path(str(checker))
        resources=list(yaml.safe_load_all(rendered))
        api=next(r for r in resources if r.get('kind')=='Deployment' and r['metadata']['name']=='eqarchives-mcp')
        env=api['spec']['template']['spec']['containers'][0]['env']
        next(e for e in env if e['name']=='EMBEDDING_URL')['value']='http://nomic-embeddings-rtx3080:8081'
        with patch('sys.stdin',io.StringIO(yaml.safe_dump_all(resources))),self.assertRaises(AssertionError):
            runpy.run_path(str(checker))
    def test_ingress_image_matches_the_ci_security_release(self):
        r=yaml.safe_load((ROOT/'scripts/security-maintenance/traefik.yaml').read_text())
        values=yaml.safe_load(r['spec']['valuesContent'])
        self.assertEqual(values['deployment']['replicas'],2)
        digest=values['image']['tag'].split('@')[1]
        self.assertIn('traefik@'+digest,(ROOT/'scripts/routing-test.env').read_text())


if __name__=='__main__':unittest.main()
