"""Verify deployment credential isolation and preservation of existing Jobs."""

import base64
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


credentials = module("curation-secrets")
guard = module("indexer-job-guard")


def encoded(value):
    return base64.b64encode(value.encode()).decode()


class DeploymentTests(unittest.TestCase):
    def test_separate_create_only_account_reuses_password_and_excludes_admin_from_output(self):
        incoming = {"items": [
            {"metadata": {"name": "elastic-password-secret"}, "data": {"ELASTIC_PASSWORD": encoded("dummy-admin")}},
            {"metadata": {"name": "eqarchives-capture-indexer-secrets"}, "data": {"es_password": encoded("dummy-writer")}},
        ]}
        requests = []
        def response(request, timeout):
            requests.append((request.full_url, request.method, json.loads(request.data)))
            self.assertEqual(timeout, 30)
            return io.StringIO('{"acknowledged":true}')
        output = io.StringIO()
        with patch.dict("os.environ", {"CURATION_ES_URL": "http://fixture:9200", "ARCHIVE_CRAWLER_OPENAI_API_KEY": "dummy-paid",
                                      "ARCHIVE_PUBLISH_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\ndummy\n"}), \
                patch("sys.stdin", io.StringIO(json.dumps(incoming))), patch.object(credentials.urllib.request, "urlopen", response), \
                redirect_stdout(output):
            credentials.main()
        self.assertEqual([method for _, method, _ in requests], ["PUT"] * 3)
        role = requests[0][2]
        self.assertEqual(role, {"cluster": [], "indices": [{"names": ["eq-archive"], "privileges": ["read", "create_doc"]}]})
        self.assertEqual(requests[1][2], {"password": "dummy-writer", "roles": ["eqarchives-capture-import"]})
        self.assertEqual(set(requests[2][2]["properties"]), {"archive_source_sha256", "archive_source_manifest", "archive_commit"})
        rendered = json.loads(output.getvalue())
        self.assertEqual([item["metadata"]["name"] for item in rendered["items"]],
                         ["eqarchives-curation-secrets", "eqarchives-capture-indexer-secrets"])
        self.assertNotIn("dummy-admin", output.getvalue())
        self.assertNotIn(encoded("dummy-admin"), output.getvalue())
        self.assertEqual(rendered["items"][1]["data"]["es_password"], encoded("dummy-writer"))
        self.assertEqual(set(rendered["items"][0]["data"]), {"luna_api_key", "archive_publish_key"})

    def test_upstream_provisioning_failure_does_not_log_response_or_render_partial_secrets(self):
        incoming = {"items": [{"metadata": {"name": "elastic-password-secret"},
                               "data": {"ELASTIC_PASSWORD": encoded("dummy-admin")}}]}
        output = io.StringIO()
        failure = urllib.error.HTTPError("http://fixture", 500, "sensitive-upstream-body", {}, None)
        with patch.dict("os.environ", {"CURATION_ES_URL": "http://fixture", "ARCHIVE_CRAWLER_OPENAI_API_KEY": "dummy-paid",
                                      "ARCHIVE_PUBLISH_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\ndummy\n"}), \
                patch("sys.stdin", io.StringIO(json.dumps(incoming))), patch.object(credentials.urllib.request, "urlopen", side_effect=failure), \
                redirect_stdout(output), self.assertRaisesRegex(SystemExit, "upstream details omitted") as caught:
            credentials.main()
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(output.getvalue(), "")

    def test_guard_hashes_sensitive_spec_and_allows_natural_completion(self):
        job = {"metadata": {"name": "existing-indexer", "uid": "original-uid"},
               "spec": {"template": {"env": {"PASSWORD": "dummy-sensitive"}}}, "status": {"active": 1}}
        snapshot = guard.snapshot({"items": [job]})
        self.assertNotIn("dummy-sensitive", json.dumps(snapshot))
        after = copy.deepcopy(job)
        after["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        with redirect_stdout(io.StringIO()):
            guard.verify(snapshot, {"items": [after]})
        for field, value in (("uid", "replacement"), ("spec", {})):
            changed = copy.deepcopy(job)
            if field == "uid":
                changed["metadata"][field] = value
            else:
                changed[field] = value
            with self.assertRaises(SystemExit):
                guard.verify(snapshot, {"items": [changed]})


if __name__ == "__main__":
    unittest.main()
