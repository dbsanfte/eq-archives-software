"""Separate create-only imports and policy updates; render secrets only to stdout.

stdin: existing Kubernetes Secret List from the trusted local kubeconfig.
stdout MUST be piped into kubectl server-side apply. Never save it or log it.
"""

import base64
import json
import os
import secrets
import sys
import urllib.request


def main():
    values = json.load(sys.stdin)
    existing = {item["metadata"]["name"]: item.get("data", {}) for item in values.get("items", [])}
    admin = base64.b64decode(existing["elastic-password-secret"]["ELASTIC_PASSWORD"]).decode()
    prior = existing.get("eqarchives-capture-indexer-secrets", {})
    password = base64.b64decode(prior["es_password"]).decode() if prior.get("es_password") else secrets.token_urlsafe(48)
    username = "eqarchives-capture-import"
    base = os.environ["CURATION_ES_URL"].rstrip("/")
    authorization = "Basic " + base64.b64encode(("elastic:" + admin).encode()).decode()
    def request_json(path, document=None):
        request = urllib.request.Request(base + path, method="PUT" if document is not None else 'GET', data=json.dumps(document).encode() if document is not None else None,
                                         headers={"Authorization": authorization, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (OSError, ValueError):
            raise SystemExit("Capture indexer credential provisioning failed; upstream details omitted") from None
    key = os.environ.get("ARCHIVE_CRAWLER_OPENAI_API_KEY", "").strip()
    ssh = os.environ.get("ARCHIVE_PUBLISH_SSH_KEY", "").strip() + "\n"
    if not key or not ssh.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):
        raise SystemExit("Missing dedicated crawler/publisher Actions secrets")
    put=request_json
    put("/_security/role/eqarchives-capture-import", {"cluster": [], "indices": [{"names": ["eq-archive"], "privileges": ["read", "create_doc"]}]})
    put("/_security/user/" + username, {"password": password, "roles": ["eqarchives-capture-import"]})
    # Only additive provenance mappings; no existing field or index changes.
    put("/eq-archive/_mapping", {"properties": {name: {"type": "keyword"} for name in
                                               ("embedding_model", "archive_source_sha256", "archive_source_manifest", "archive_commit", "llm_enrichment_signature")}})
    aliases=request_json('/_alias/eq-archive')
    if not isinstance(aliases,dict) or len(aliases)!=1:
        raise SystemExit('Policy maintenance requires exactly one archive index; no credentials rendered')
    index=next(iter(aliases))
    import re
    if not re.fullmatch(r'eq-archive-[0-9][a-zA-Z0-9._-]*',index):
        raise SystemExit('Unexpected archive index identity; no credentials rendered')
    policy_user='eqarchives-policy-maintenance'
    prior_policy=existing.get('eqarchives-policy-indexer-secrets',{})
    policy_password=base64.b64decode(prior_policy['es_password']).decode() if prior_policy.get('es_password') else secrets.token_urlsafe(48)
    put('/_security/role/'+policy_user,{'cluster':[],'indices':[{'names':[index],'privileges':['read','index']}]})
    put('/_security/user/'+policy_user,{'password':policy_password,'roles':[policy_user]})
    def secret(name, fields):
        return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "eqarchives-es"},
                "type": "Opaque", "data": {name: base64.b64encode(value.encode()).decode() for name, value in fields.items()}}
    json.dump({"apiVersion": "v1", "kind": "List", "items": [
        secret("eqarchives-curation-secrets", {"luna_api_key": key, "archive_publish_key": ssh}),
        secret("eqarchives-capture-indexer-secrets", {"es_username": username, "es_password": password}),
        secret('eqarchives-policy-indexer-secrets',{'es_username':policy_user,'es_password':policy_password,'es_index':index})]}, sys.stdout)


if __name__ == "__main__":
    main()
