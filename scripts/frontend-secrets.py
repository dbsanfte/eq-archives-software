"""Render runtime Kubernetes secrets to a pipe; never save this output in git."""

import base64
import json
import os
import sys


def required(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def secret(name, values, secret_type="Opaque"):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": "eqarchives-es"},
        "type": secret_type,
        "data": {
            key: base64.b64encode(value.encode()).decode()
            for key, value in values.items()
        },
    }


username = required("DOCKERHUB_USERNAME")
token = required("DOCKERHUB_TOKEN")
registry_config = json.dumps({
    "auths": {
        "https://index.docker.io/v1/": {
            "auth": base64.b64encode(f"{username}:{token}".encode()).decode()
        }
    }
})
json.dump({
    "apiVersion": "v1",
    "kind": "List",
    "items": [
        secret("search-eqarchives-secrets", {
            "es_readonly_username": required("FRONTEND_ES_USERNAME"),
            "es_readonly_password": required("FRONTEND_ES_PASSWORD"),
            "openai_api_key": required("FRONTEND_OPENAI_API_KEY"),
        }),
        secret("dockerhub-pull-secret", {".dockerconfigjson": registry_config},
               "kubernetes.io/dockerconfigjson"),
    ],
}, sys.stdout)
