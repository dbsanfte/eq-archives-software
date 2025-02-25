# es_manager.py

import os
import logging
import datetime
from elasticsearch import Elasticsearch, exceptions

logger = logging.getLogger(__name__)

# A single dictionary describing each field and its ES mapping configuration
ES_FIELDS = {
    "id": {
        "mapping": {"type": "keyword"},
        "default": None
    },
    "parent_id": {
        "mapping": {"type": "keyword"},
        "default": None
    },
    "last_indexed": {
        "mapping": {"type": "date"},
        "default": None  # We'll set this later if None
    },
    "title": {
        "mapping": {"type": "text"},
        "default": None
    },
    "thumbnail": {
        "mapping": {"type": "text"},
        "default": None
    },
    "mime_type": {
        "mapping": {"type": "keyword"},
        "default": None
    },
    "file_type": {
        "mapping": {"type": "keyword"},
        "default": None
    },
    "text": {
        "mapping": {
            "type": "nested",
            "properties": {
                "vector": {
                    "type": "dense_vector",
                    "dims": 768,
                    "index": True,
                    "index_options": {"type": "int8_hnsw"}
                },
                "text_chunk": {"type": "text"}
            }
        },
        "default": None
    },
    "text_full": {
        "mapping": {"type": "text"},
        "default": None
    },
    "llm_model_name": {
        "mapping": {"type": "keyword"},
        "default": None
    },
    "llm_summary": {
        "mapping": {"type": "text"},
        "default": None
    },
    "llm_summary_vector": {
        "mapping": {
            "type": "dense_vector",
            "dims": 768,
            "index": True,
            "index_options": {"type": "int8_hnsw"}
        },
        "default": None
    },
    "llm_content_flavour": {
        "mapping": {"type": "keyword"},
        "default": None
    },
    "llm_tags": {
        "mapping": {"type": "keyword"},
        "default": None
    },
    "llm_guessed_date": {
        "mapping": {"type": "date"},
        "default": None
    },
    "llm_extracted_dates": {
        "mapping": {
            "type": "nested",
            "properties": {
                "date": {"type": "date"}
            }
        },
        "default": None
    },
    "llm_image_text_full": {
        "mapping": {"type": "text"},
        "default": None
    },
    "llm_image_text_vector": {
        "mapping": {
            "type": "dense_vector",
            "dims": 768,
            "index": True,
            "index_options": {"type": "int8_hnsw"}
        },
        "default": None
    },
    "domain_name": {
        "mapping": {"type": "keyword"},
        "default": None
    },
    "capture_date": {
        "mapping": {"type": "date"},
        "default": None
    },
    "mailing_list_name": {
        "mapping": {"type": "keyword"},
        "default": None
    },
    "url": {
        "mapping": {"type": "text"},
        "default": None
    },
    "alternate_url": {
        "mapping": {"type": "text"},
        "default": None
    }
}

class ElasticsearchManager:
    _index_template_created = False
    _index_created = False
    
    def __init__(
        self, 
        es_client: Elasticsearch=None, 
        host=None, 
        port=None, 
        es_username=None, 
        es_password=None, 
        es_password_file="/run/secrets/es_password", 
        es_username_file="/run/secrets/es_username", 
        index_name=None, 
        es_max_retries=3, 
        es_request_timeout=300, 
        ssl_verify_certs=True, 
        ssl_show_warn=True
    ):
        self._host = host or os.environ.get("ELASTICSEARCH_HOST", "http://elasticsearch")
        self._port = port or os.environ.get("ELASTICSEARCH_PORT", "9200")
        
        self._es_username = es_username or os.environ.get("ELASTICSEARCH_USERNAME")
        self._es_password = es_password or os.environ.get("ELASTICSEARCH_PASSWORD")
        if os.path.exists(es_username_file):
            with open(es_username_file, "r") as f:
                self._es_username = f.read().strip()
        if os.path.exists(es_password_file):
            with open(es_password_file, "r") as f:
                self._es_password = f.read().strip()
        
        self._index_name = index_name or os.environ.get("ELASTICSEARCH_INDEX", "eq-archive")
        self._es_max_retries = os.environ.get("ELASTICSEARCH_MAX_RETRIES", es_max_retries)
        self._es_retry_on_timeout = os.environ.get("ELASTICSEARCH_RETRY_ON_TIMEOUT", True)
        self._es_request_timeout = os.environ.get("ELASTICSEARCH_REQUEST_TIMEOUT", es_request_timeout)
        self._ssl_verify_certs = os.environ.get("ELASTICSEARCH_SSL_VERIFY_CERTS", ssl_verify_certs)
        self._ssl_show_warn = os.environ.get("ELASTICSEARCH_SSL_SHOW_WARN", ssl_show_warn)
        self._es_client = es_client

    def _create_client(self):
        if self._es_client is not None:
            if self._es_client.ping():
                logger.debug("Reusing existing Elasticsearch client.")
                return self._es_client
            else:
                logger.warning("Existing Elasticsearch client is unavailable, reconnecting...")
        
        es = Elasticsearch(
            f"{self._host}:{self._port}",
            verify_certs=self._ssl_verify_certs,
            ssl_show_warn=self._ssl_show_warn,
            retry_on_timeout=self._es_retry_on_timeout,
            max_retries=self._es_max_retries,
            retry_on_status=[502, 503, 504],
            request_timeout=self._es_request_timeout,
            http_auth=(self._es_username, self._es_password)
        )
        if not es.ping():
            raise exceptions.ConnectionError("Could not ping Elasticsearch.")
        logger.info("Successfully connected to Elasticsearch.")
        self._es_client = es
        self._create_index_template()
        self._initialize_es_index()
        return es

    def get_client(self):
        if self._es_client is None:
            logger.debug("Creating new Elasticsearch client...")
            self._es_client = self._create_client()
        try:
            self._es_client.ping()
        except exceptions.ConnectionError:
            logger.warning("Elasticsearch client is unavailable, reconnecting...")
            self._es_client = self._create_client()
        return self._es_client

    def _create_index_template(self):
        es = self.get_client()
        if not self._index_template_created:
            # Build ES mappings from our single ES_FIELDS dict
            template_body = {
                "index_patterns": ["eq-archive*"],
                "template": {
                    "settings": {"number_of_shards": 1},
                    "mappings": {
                        "properties": {
                            field_name: cfg["mapping"] for field_name, cfg in ES_FIELDS.items()
                        }
                    }
                }
            }
            template_name = "eq-archive-index-template"
            es.indices.put_index_template(name=template_name, body=template_body)
        self._index_template_created = True

    def _initialize_es_index(self):
        es = self.get_client()
        if not self._index_created:
            if not es.indices.exists(index=self._index_name):
                logger.debug(f"Creating Elasticsearch index as it doesn't exist yet: {self._index_name}")
                response = es.indices.create(index=self._index_name)
                logger.debug(f"Index created: {response}")
            else:
                logger.debug(f"Index already exists: {self._index_name}")
            self._index_created = True

    def index_document(self, doc_id, doc_body):
        es = self.get_client()
        try:
            logger.debug(f"Indexing document: {doc_id}")
            es.index(index=self._index_name, id=doc_id, op_type="index", body=doc_body)
        except exceptions.RequestError as e:
            logger.error(f"Error indexing document: {e}")
            logger.exception(e)

    def record_exists(self, doc_id):
        es = self.get_client()
        try:
            return es.exists(index=self._index_name, id=doc_id)
        except exceptions.RequestError as e:
            logger.error(f"Error checking if record exists: {e}")
            logger.exception(e)
            return False

    def get_document(self, doc_id):
        es = self.get_client()
        try:
            return es.get(index=self._index_name, id=doc_id)
        except exceptions.RequestError as e:
            logger.error(f"Error fetching document: {e}")
            logger.exception(e)
            return None

    def chunks_exist(self, prefix):
        """
        Check if any document IDs start with a given prefix (for chunked docs).
        """
        es = self.get_client()
        try:
            body = {
                "query": {
                    "prefix": {
                        "id": prefix
                    }
                }
            }
            result = es.search(index=self._index_name, body=body, size=1)
            return result.get("hits", {}).get("total", {}).get("value", 0) > 0
        except exceptions.RequestError as e:
            logger.error(f"Error searching for chunks: {e}")
            logger.exception(e)
            return False

    @staticmethod
    def build_document(**kwargs):
        # Identify any fields passed in that don't exist in ES_FIELDS
        invalid_fields = set(kwargs.keys()) - set(ES_FIELDS.keys())
        if invalid_fields:
            raise ValueError(f"Invalid field(s): {', '.join(invalid_fields)}")

        doc = {}
        for field_name, config in ES_FIELDS.items():
            doc[field_name] = kwargs.get(field_name, config.get("default"))

        # If last_indexed wasn't passed in, set the current time
        if not doc["last_indexed"]:
            doc["last_indexed"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        return doc