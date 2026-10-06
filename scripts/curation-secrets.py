"""Provision a separate create-only ES account; render secrets only to stdout.

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
    def put(path, document):
        request = urllib.request.Request(base + path, method="PUT", data=json.dumps(document).encode(),
                                         headers={"Authorization": authorization, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                json.load(response)
        except (OSError, ValueError):
            raise SystemExit("Capture indexer credential provisioning failed; upstream details omitted") from None
    key = os.environ.get("ARCHIVE_CRAWLER_OPENAI_API_KEY", "").strip()
    ssh = os.environ.get("ARCHIVE_PUBLISH_SSH_KEY", "").strip() + "\n"
    if not key or not ssh.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):
        raise SystemExit("Missing dedicated crawler/publisher Actions secrets")
    put("/_security/role/eqarchives-capture-import", {"cluster": [], "indices": [{"names": ["eq-archive"], "privileges": ["read", "create_doc"]}]})
    put("/_security/user/" + username, {"password": password, "roles": ["eqarchives-capture-import"]})
    # Only additive provenance mappings; no existing field or index changes.
    put("/eq-archive/_mapping", {"properties": {name: {"type": "keyword"} for name in
                                               ("archive_source_sha256", "archive_source_manifest", "archive_commit")}})
    def secret(name, fields):
        return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "eqarchives-es"},
                "type": "Opaque", "data": {name: base64.b64encode(value.encode()).decode() for name, value in fields.items()}}
    json.dump({"apiVersion": "v1", "kind": "List", "items": [
        secret("eqarchives-curation-secrets", {"luna_api_key": key, "archive_publish_key": ssh}),
        secret("eqarchives-capture-indexer-secrets", {"es_username": username, "es_password": password})]}, sys.stdout)


if __name__ == "__main__":
    main()
