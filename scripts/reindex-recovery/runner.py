"""Run the checkpointed text repair through temporary cluster outages."""
import base64
import json
import logging
import os
from pathlib import Path
import runpy
import sys
import time
import urllib.error
import urllib.request

LOG = logging.getLogger('reindex-recovery')
UPSTREAM_FAILURE = 'Repeated upstream failures; retry resumes completed versions'


class ConfigurationError(Exception):
    """A correction is required; retrying cannot repair credentials/configuration."""


def request_ready(url, headers, *, models=False, opener=urllib.request.urlopen):
    try:
        with opener(urllib.request.Request(url, headers=headers), timeout=10) as response:
            result = json.load(response)
        if models:
            return isinstance(result.get('data'), list) and bool(result['data'])
        return result.get('status') in ('ok', 'yellow', 'green') and not result.get('timed_out')
    except urllib.error.HTTPError as error:
        if error.code in (408, 429) or error.code >= 500:
            return False
        raise ConfigurationError('Dependency rejected the readiness check; verify configuration and credentials') from None
    except (OSError, urllib.error.URLError, ValueError):
        return False


def dependencies_ready():
    secret_dir = Path('/run/secrets')
    try:
        password = (secret_dir / 'es_password').read_text().strip()
        key = (secret_dir / 'openai_api_key').read_text().strip()
    except OSError:
        raise ConfigurationError('Required dependency credential files are unavailable') from None
    if not password or not key:
        raise ConfigurationError('Required dependency credentials are empty')
    username = os.environ.get('ELASTICSEARCH_USERNAME', 'elastic')
    basic = base64.b64encode(f'{username}:{password}'.encode()).decode()
    host = os.environ['ELASTICSEARCH_HOST'].rstrip('/')
    port = os.environ.get('ELASTICSEARCH_PORT', '9200')
    model = os.environ['OPENAI_ENDPOINT'].rstrip('/').removesuffix('/v1')
    return (request_ready(f'{host}:{port}/_cluster/health?wait_for_status=yellow&timeout=5s',
                          {'Authorization': 'Basic ' + basic}) and
            request_ready(model + '/v1/models', {'Authorization': 'Bearer ' + key}, models=True))


def is_transient(error):
    # Import from the pinned worker image; this script adds no ML dependencies.
    from elastic_transport import TransportError
    from elasticsearch import ApiError
    from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
    return (isinstance(error, (TransportError, APIConnectionError, APITimeoutError,
                              InternalServerError, RateLimitError)) or
            isinstance(error, ApiError) and (error.status_code == 429 or error.status_code >= 500) or
            isinstance(error, RuntimeError) and str(error) == UPSTREAM_FAILURE
            and error.__context__ is not None and is_transient(error.__context__))


def execute_worker():
    entrypoint = Path(os.environ.get('REINDEX_ENTRYPOINT', 'reindex_text.pyc')).resolve()
    # This runner is mounted outside /app/src. Preserve the original script's
    # sibling-module import path, including on each checkpointed continuation.
    parent = str(entrypoint.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return runpy.run_path(str(entrypoint), run_name='__main__')


def run(*, ready=dependencies_ready, execute=None, transient=is_transient, sleep=time.sleep):
    execute = execute or execute_worker
    delay = 5
    while True:
        if not ready():
            LOG.warning('Waiting for Elasticsearch/DNS and Nomic readiness; retry in %ss', delay)
        else:
            LOG.info('Dependencies ready; resuming remaining extraction/chunking checkpoints')
            try:
                execute()
                return 0
            except SystemExit as error:
                # Success and content/integrity failures keep the worker's exit
                # status. Only infrastructure exceptions are retried here.
                return error.code or 0
            except Exception as error:
                if not transient(error):
                    raise
                # Never log an upstream payload: it can contain credentials.
                LOG.warning('Temporary dependency failure (%s); checkpoints retained; retry in %ss',
                            type(error).__name__, delay)
        sleep(delay)
        delay = min(delay * 2, 60)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        return run()
    except ConfigurationError as error:
        LOG.error('%s', error)
        return 42


if __name__ == '__main__':
    sys.exit(main())
