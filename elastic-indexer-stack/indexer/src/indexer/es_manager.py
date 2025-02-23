# es_manager.py

import os
import logging
import datetime
from elasticsearch import Elasticsearch, exceptions

logger = logging.getLogger(__name__)

class ElasticsearchManager:
    _index_template_created = False
    _index_created = False
    
    def __init__(self, es_client: Elasticsearch=None, host=None, port=None, es_username=None, es_password=None, 
                 es_password_file="/run/secrets/es_password", es_username_file="/run/secrets/es_username", index_name=None, 
                 es_max_retries=3, es_request_timeout=300, ssl_verify_certs=True, ssl_show_warn=True):
        self._host=host or os.environ.get("ELASTICSEARCH_HOST", "http://elasticsearch")
        self._port=port or os.environ.get("ELASTICSEARCH_PORT", "9200")
        
        self._es_username=es_username or os.environ.get("ELASTICSEARCH_USERNAME")
        self._es_password=es_password or os.environ.get("ELASTICSEARCH_PASSWORD")
        if os.path.exists(es_username_file):
            with open(es_username_file, "r") as f:
                self._es_username = f.read().strip()
        if os.path.exists(es_password_file):
            with open(es_password_file, "r") as f:
                self._es_password = f.read().strip()
        
        self._index_name=index_name or os.environ.get("ELASTICSEARCH_INDEX", "eq-archive")
        self._es_max_retries = os.environ.get("ELASTICSEARCH_MAX_RETRIES", es_max_retries)
        self._es_retry_on_timeout = os.environ.get("ELASTICSEARCH_RETRY_ON_TIMEOUT", True)
        self._es_request_timeout = os.environ.get("ELASTICSEARCH_REQUEST_TIMEOUT", es_request_timeout)
        self._ssl_verify_certs = os.environ.get("ELASTICSEARCH_SSL_VERIFY_CERTS", ssl_verify_certs)
        self._ssl_show_warn = os.environ.get("ELASTICSEARCH_SSL_SHOW_WARN", ssl_show_warn)
        self._es_client = es_client  # Will be initialized on demand

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

    # DS - Multi vector documents in Elasticsearch:
    # https://www.elastic.co/search-labs/blog/multi-vector-documents
    def _create_index_template(self):
        es = self.get_client()
        
        if not self._index_template_created:
            template_body = {
                "index_patterns": ["eq-archive*"],
                "template": {
                    "settings": {
                        "number_of_shards": 1
                    },
                    "mappings": {
                        "properties": {
                            "id": {
                                "type": "keyword"
                            },
                            "parent_id": {
                                "type": "keyword"
                            },
                            "last_indexed": {
                                "type": "date"
                            },
                            "title": {
                                "type": "text"
                            },
                            "thumbnail": {
                                "type": "text"
                            },
                            "mime_type": {
                                "type": "keyword"
                            },
                            "file_type": {
                                "type": "keyword"
                            },
                            "text": {
                                "type": "nested",
                                "properties": {
                                    "vector": {
                                        "type": "dense_vector",
                                        "dims": 768,
                                        "index": True,
                                        "index_options": {
                                            "type": "int8_hnsw"
                                        }
                                    },
                                    "text_chunk": {
                                        "type": "text"
                                    }
                                }
                            },
                            "text_full": {
                                "type": "text"
                            },
                            "llm_model_name": {
                                "type": "keyword"  
                            },
                            "llm_summary": {
                                "type": "text"
                            },
                            "llm_summary_vector": {
                                "type": "dense_vector",
                                "dims": 768,
                                "index": True,
                                "index_options": {
                                    "type": "int8_hnsw"
                                }
                            },
                            "llm_content_flavour": {
                                "type": "keyword"
                            },
                            "llm_tags": {
                                "type": "keyword"
                            },
                            "llm_guessed_date": {
                                "type": "date"  
                            },
                            "llm_image_text": {
                                "type": "text"  
                            },
                            "llm_image_text_vector": {
                                "type": "dense_vector",
                                "dims": 768,
                                "index": True,
                                "index_options": {
                                    "type": "int8_hnsw"
                                }  
                            },
                            "domain_name": {
                                "type": "keyword"
                            },
                            "capture_date": {
                                "type": "date"
                            },
                            "mailing_list_name": {
                                "type": "keyword"
                            },
                            "url": {
                                "type": "text"
                            },
                            "alternate_url": {
                                "type": "text"
                            }
                        }
                    }
                }
            }
            
            template_name = "eq-archive-index-template"
            logging.info(f"Ensuring index template created: {template_name}")
            response = es.indices.put_index_template(name=template_name, body=template_body)
            logging.debug(f"Index template up to date: {response}")
            
        self._index_template_created = True

    def _initialize_es_index(self):
        es = self.get_client()
        if not self._index_created:
            if not es.indices.exists(index=self._index_name):
                logging.debug(f"Creating Elasticsearch index as it doesn't exist yet: {self._index_name}")
                response = es.indices.create(index=self._index_name)
                logging.debug(f"Index created: {response}")
            else:
                logging.debug(f"Index already exists: {self._index_name}")
            self._index_created = True

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
    def build_document(
        id: str,
        title: str = None,
        file_type: str = None,
        mime_type: str = None,
        text: list = None,
        text_full: str = None,
        llm_summary: str = None,
        llm_summary_vector = None,
        llm_guessed_date = None,
        llm_model_name: str = None,
        llm_content_flavour: str = None,
        llm_tags: list = None,
        llm_image_text_vector: list = None,
        llm_image_text_full: str = None,
        domain_name: str = None,
        mailing_list_name: str = None,
        url: str = None,
        alternate_url: str = None,
        thumbnail: str = None
    ) -> dict:
        return {
            "id": id,
            "last_indexed": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "title": title,
            "file_type": file_type,
            "mime_type": mime_type,
            "text": text,
            "text_full": text_full,
            "llm_summary": llm_summary,
            "llm_summary_vector": llm_summary_vector,
            "llm_guessed_date": llm_guessed_date,
            "llm_model_name": llm_model_name,
            "llm_content_flavour": llm_content_flavour,
            "llm_tags": llm_tags,
            "domain_name": domain_name,
            "mailing_list_name": mailing_list_name,
            "url": url,
            "alternate_url": alternate_url,
            "thumbnail": thumbnail
        }