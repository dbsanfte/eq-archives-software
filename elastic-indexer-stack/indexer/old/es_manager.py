# new file
import logging
from elasticsearch import Elasticsearch, exceptions
from tenacity import retry, wait_exponential, stop_after_attempt, before_sleep_log

logger = logging.getLogger(__name__)

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(10),
    before_sleep=before_sleep_log(logging, logging.WARNING),
    reraise=True
)
def create_es_client(hosts, **kwargs):
    es = Elasticsearch(hosts, **kwargs)
    if not es.ping():
        raise exceptions.ConnectionError("Could not ping Elasticsearch.")
    logger.debug("Successfully connected to Elasticsearch.")
    return es

def connect_elasticsearch(host, port):
    logger.debug("Trying to connect to Elasticsearch...")
    return create_es_client(
        f"http://{host}:{port}",
        verify_certs=False,
        ssl_show_warn=False,
        retry_on_timeout=True,
        max_retries=3,
        retry_on_status=[502, 503, 504],
        request_timeout=300
    )

def get_es_client(host, port):
    if not hasattr(get_es_client, "client"):
        logger.debug("Creating new Elasticsearch client...")
        get_es_client.client = connect_elasticsearch(host, port)
    try:
        get_es_client.client.ping()
    except exceptions.ConnectionError:
        logger.warning("Elasticsearch client is unavailable, reconnecting...")
        get_es_client.client = connect_elasticsearch(host, port)
    return get_es_client.client

def index_document(es, index_name, doc_id, doc_body):
    try:
        es.index(index=index_name, id=doc_id, op_type="index", body=doc_body)
    except exceptions.RequestError as e:
        logger.error(f"Error indexing document: {e}")
        logger.exception(e)