"""Read existing Jobs; create deterministic import Jobs only when ingestion is idle."""

import json
import os
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.request

from common import CrawlError


class Kubernetes:
    def __init__(self):
        directory = Path(os.environ.get("KUBERNETES_CREDENTIALS_DIR", "/var/run/secrets/kubernetes.io/serviceaccount"))
        self.directory = directory
        self.namespace = directory.joinpath("namespace").read_text().strip()
        self.base = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
        self.context = ssl.create_default_context(cafile=str(directory / "ca.crt"))

    def request(self, method, path, value=None):
        token = (self.directory / "token").read_text().strip()  # Tokens rotate.
        data = json.dumps(value).encode() if value is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 409:
                return None
            raise CrawlError(f"Kubernetes HTTP {error.code}; Job submission paused") from None
        except (OSError, ValueError):
            raise CrawlError("Kubernetes is unavailable; Job submission paused") from None

    def jobs(self):
        result = []
        continuation = ""
        from urllib.parse import urlencode
        while True:
            path = f"/apis/batch/v1/namespaces/{self.namespace}/jobs?" + urlencode({"limit": 100, "continue": continuation})
            response = self.request("GET", path)
            result.extend(response["items"])
            continuation = response.get("metadata", {}).get("continue", "")
            if not continuation:
                return result

    def create(self, job):
        return self.request("POST", f"/apis/batch/v1/namespaces/{self.namespace}/jobs", job)


def finished(job):
    return any(condition.get("type") in ("Complete", "Failed") and condition.get("status") == "True"
               for condition in job.get("status", {}).get("conditions", []))


def blockers(jobs, own_name):
    # Conservatively wait for every other unfinished, unsuspended Job in this
    # namespace, including Jobs awaiting scheduling or retrying with active=0.
    return sorted(job["metadata"]["name"] for job in jobs
                  if job["metadata"]["name"] != own_name
                  and not finished(job) and not job["spec"].get("suspend", False))


def import_attempt(batch):
    attempt = (batch.get('job') or {}).get('attempt', 1)
    if type(attempt) is not int or not 1 <= attempt <= 999999:
        raise CrawlError('Invalid indexing attempt')
    return attempt


def import_name(batch):
    attempt = import_attempt(batch)
    return 'eqarchives-captures-' + batch['id'] + (f'-r{attempt}' if attempt > 1 else '')


def import_job(batch, image):
    if not re.fullmatch(r"dbsanfte/frontend@sha256:[a-f0-9]{64}", image):
        raise CrawlError("Targeted indexing requires the immutable deployed curation image")
    batch_id = batch["id"]
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": import_name(batch),
                         "namespace": "eqarchives-es", "labels": {"app": "eqarchives-capture-import"},
                         "annotations": {"eqarchives.org/manifest-sha256": batch["manifest_sha256"],
                                         "eqarchives.org/import-attempt": str(import_attempt(batch))}},
            "spec": {"backoffLimit": 2, "activeDeadlineSeconds": 21600,
                     "template": {"metadata": {"labels": {"app": "eqarchives-capture-import"}},
                                  "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                           "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                                               "runAsGroup": 10001, "fsGroup": 10001,
                                                               "fsGroupChangePolicy": "OnRootMismatch",
                                                               "seccompProfile": {"type": "RuntimeDefault"}},
                                           "imagePullSecrets": [{"name": "dockerhub-pull-secret"}],
                                           "containers": [{"name": "index", "image": image,
                                               "command": ["python", "/app/index_captures.py", "--root", "/data",
                                                           "--batch", f"batches/{batch_id}/approved.json",
                                                           "--manifest-sha256", batch["manifest_sha256"]],
                                               "env": [{"name": "ELASTICSEARCH_URL", "value": "http://elasticsearch.eqarchives-es.svc.cluster.local:9200"},
                                                       {"name": "IMPORT_JOB_NAME", "value": import_name(batch)},
                                                       {"name": "EMBEDDING_URL", "value": "http://nomic-embeddings.eqarchives-es.svc.cluster.local:8080"},
                                                       {"name": "EMBEDDING_MODEL", "value": "text-embedding-nomic-embed-text-v1.5@q8_0"}],
                                               "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                                             "limits": {"cpu": "1", "memory": "512Mi"}},
                                               "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                                   "capabilities": {"drop": ["ALL"]}},
                                               "volumeMounts": [{"name": "staging", "mountPath": "/data", "readOnly": True},
                                                                {"name": "credentials", "mountPath": "/run/secrets", "readOnly": True},
                                                                {"name": "staging", "mountPath": "/enrichment", "subPath": "enrichment"}]}],
                                           "volumes": [{"name": "staging", "persistentVolumeClaim": {"claimName": "eqarchives-curation"}},
                                                       {"name": "credentials", "projected": {"defaultMode": 288,
                                                        "sources": [{"secret": {"name": "eqarchives-capture-indexer-secrets",
                                                                    "items": [{"key": "es_username", "path": "es_username"},
                                                                              {"key": "es_password", "path": "es_password"}]}},
                                                                    {"secret": {"name": "search-eqarchives-secrets",
                                                                    "items": [{"key": "openai_api_key", "path": "openai_api_key"}]}},
                                                                    {"secret": {"name": "eqarchives-curation-secrets",
                                                                    "items": [{"key": "luna_api_key", "path": "luna_api_key"}]}}]}}]}}}}
