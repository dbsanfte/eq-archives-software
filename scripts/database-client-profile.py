"""Persist/read the operator's database transport profile without exposing keys.

Only public CA certificates are stored here. A missing profile bootstraps the
legacy HTTP endpoint; existing HTTPS profiles are never rewritten by deployment.
Output is a shell-quoted environment file and a public CA bundle in a private
work directory. Client pods mount this same bundle over certifi's trust store,
so pinned images and clients using trust_env=False still verify the private CA.
"""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import shlex
import ssl
import subprocess
from urllib.parse import urlsplit

NAME='eqarchives-database-client'
URL='http://elasticsearch.eqarchives-es.svc.cluster.local:9200'
BUNDLE_PATH='/usr/local/lib/python3.13/site-packages/certifi/cacert.pem'
K=['sudo','-n','kubectl','--kubeconfig=/etc/rancher/k3s/k3s.yaml','-n','eqarchives-es']


def validate(profile):
    data=profile['data'];url=urlsplit(data['url'])
    if (url.scheme not in ('http','https') or url.hostname!='elasticsearch.eqarchives-es.svc.cluster.local'
        or url.port!=9200 or url.path not in ('','/') or url.username or url.password or url.query or url.fragment):
        raise ValueError('Invalid database transport profile')
    if data.get('host')!=url.scheme+'://'+url.hostname or data.get('port')!='9200':
        raise ValueError('Inconsistent database transport profile')
    ssl.create_default_context(cadata=data['ca-bundle.pem'])
    return data


def load(kube=None,public_roots=None):
    kube=kube or subprocess.run
    result=kube(K+['get','configmap',NAME,'--ignore-not-found','-o','json'],capture_output=True,text=True,check=True)
    if result.stdout.strip():return validate(json.loads(result.stdout))
    roots=public_roots or Path('/etc/ssl/certs/ca-certificates.crt').read_text()
    profile={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':NAME,'namespace':'eqarchives-es'},
             'data':{'url':URL,'host':'http://elasticsearch.eqarchives-es.svc.cluster.local','port':'9200','ca-bundle.pem':roots}}
    validate(profile)
    # Create, not apply: a concurrent operator's HTTPS choice must never be reset.
    result=kube(K+['create','-f','-'],input=json.dumps(profile),capture_output=True,text=True)
    if result.returncode:
        result=kube(K+['get','configmap',NAME,'-o','json'],capture_output=True,text=True,check=True)
        return validate(json.loads(result.stdout))
    return profile['data']


def export(directory,data,service_ip):
    directory=Path(directory);ipaddress.ip_address(service_ip)
    bundle=directory/'database-ca-bundle.pem';bundle.write_text(data['ca-bundle.pem']);bundle.chmod(0o600)
    scheme=urlsplit(data['url']).scheme
    values={'DATABASE_CLIENT_URL':data['url'],'DATABASE_OPERATOR_URL':f'{scheme}://{service_ip}:9200',
            'DATABASE_CLIENT_CHECKSUM':hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest(),
            'SSL_CERT_FILE':str(bundle),'CURL_CA_BUNDLE':str(bundle),'REQUESTS_CA_BUNDLE':str(bundle)}
    env=directory/'database-client.env';env.write_text(''.join('export '+k+'='+shlex.quote(v)+'\n' for k,v in values.items()));env.chmod(0o600)
    return env


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',type=Path,required=True);parser.add_argument('--service-ip',required=True)
    args=parser.parse_args();export(args.directory,load(),args.service_ip)
