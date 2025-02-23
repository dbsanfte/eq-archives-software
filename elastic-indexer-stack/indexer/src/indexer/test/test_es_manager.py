# test_es_manager.py

import pytest
from unittest.mock import Mock, patch
from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig
from elasticsearch.exceptions import ConnectionError, RequestError, TransportError
from indexer.es_manager import ElasticsearchManager
from elasticsearch.exceptions import RequestError

@pytest.fixture
def mock_elasticsearch():
    with patch("indexer.es_manager.Elasticsearch") as mock_es_class:
        instance = Mock()
        mock_es_class.return_value = instance
        yield instance

@pytest.fixture
def manager(mock_elasticsearch):
    return ElasticsearchManager(host="test-host", port=9200, index_name="test-index", 
                                ssl_show_warn=False, ssl_verify_certs=False, es_client=None)

def test_create_client_success(manager, mock_elasticsearch):
    mock_elasticsearch.ping.return_value = True
    mock_elasticsearch.indices.exists_index_template.return_value = False
    mock_elasticsearch.indices.exists.return_value = False

    es_client = manager._create_client()

    assert es_client is not None
    mock_elasticsearch.ping.assert_called()
    mock_elasticsearch.indices.put_index_template.assert_called_once()
    mock_elasticsearch.indices.create.assert_called_once_with(index="test-index")

@patch("indexer.es_manager.Elasticsearch.tenacity_max_attempts", 1)
@patch("indexer.es_manager.Elasticsearch.tenacity_max_exponential_backoff", 1)
@patch("indexer.es_manager.Elasticsearch.tenacity_min_exponential_backoff", 1)
@patch("indexer.es_manager.Elasticsearch.tenacity_backoff_multiplier", 1)
def test_create_client_fail(mock_elasticsearch, manager):
    with pytest.raises(ConnectionError):
        mock_elasticsearch._retry_on_timeout = False
        mock_elasticsearch.ping.return_value = False
        manager._create_client()
    mock_elasticsearch.ping.assert_called()

def test_get_client_creates_new(manager, mock_elasticsearch):
    mock_elasticsearch.ping.return_value = True
    client = manager.get_client()
    assert client is not None
    mock_elasticsearch.ping.assert_called()

def test_get_client_reconnects(manager, mock_elasticsearch):
    # Assign the current client
    manager._es_client = mock_elasticsearch
    # Simulate a ping failure
    mock_elasticsearch.ping.side_effect = ConnectionError("Test reconnect")

    with patch.object(manager, "_create_client", return_value="new_mock_client") as mock_create:
        # Should reconnect upon ping failure
        client = manager.get_client()
        mock_create.assert_called_once()
        assert client == "new_mock_client"

def test_index_document_success(manager, mock_elasticsearch):
    manager.index_document("doc_id", {"key": "value"})
    mock_elasticsearch.index.assert_called_with(
        index="test-index", id="doc_id", op_type="index", body={"key": "value"}
    )

def test_index_document_request_error(manager, mock_elasticsearch, caplog):
    mock_elasticsearch.index.side_effect = RequestError(
        400, ApiResponseMeta(400, "1.1", HttpHeaders(), 1, NodeConfig("http", "localhost", 1234)), {"error": "Error"}
    )
    manager.index_document("doc_id", {"key": "value"})
    assert any("Error indexing document:" in message for message in caplog.messages)

def test_record_exists_success(manager, mock_elasticsearch):
    mock_elasticsearch.exists.return_value = True
    result = manager.record_exists("doc_id")
    assert result is True

def test_record_exists_request_error(manager, mock_elasticsearch, caplog):
    mock_elasticsearch.exists.side_effect = RequestError(
        400, ApiResponseMeta(400, "1.1", HttpHeaders(), 1, NodeConfig("http", "localhost", 1234)), {"error": "Error"}
    )
    result = manager.record_exists("doc_id")
    assert result is False
    assert any("Error checking if record exists:" in message for message in caplog.messages)

def test_get_document_success(manager, mock_elasticsearch):
    mock_elasticsearch.get.return_value = {"_source": {"key": "value"}}
    result = manager.get_document("doc_id")
    assert result == {"_source": {"key": "value"}}

def test_get_document_request_error(manager, mock_elasticsearch, caplog):
    mock_elasticsearch.get.side_effect = RequestError(
        400, ApiResponseMeta(400, "1.1", HttpHeaders(), 1, NodeConfig("http", "localhost", 1234)), {"error": "Error"}
    )
    result = manager.get_document("doc_id")
    assert result is None
    assert any("Error fetching document:" in message for message in caplog.messages)

def test_create_index_template_new(manager, mock_elasticsearch):
    mock_elasticsearch.ping.return_value = True
    mock_elasticsearch.indices.exists_index_template.return_value = False
    manager._create_index_template()
    mock_elasticsearch.indices.put_index_template.assert_called_once()

def test_create_index_template_existing(manager, mock_elasticsearch):
    mock_elasticsearch.ping.return_value = True
    manager._create_index_template()
    manager._create_index_template()
    mock_elasticsearch.indices.put_index_template.assert_called_once()

def test_initialize_es_index_new(manager, mock_elasticsearch):
    mock_elasticsearch.ping.return_value = True
    mock_elasticsearch.indices.exists.return_value = False
    manager._initialize_es_index()
    mock_elasticsearch.indices.create.assert_called_once_with(index="test-index")

def test_initialize_es_index_existing(manager, mock_elasticsearch):
    mock_elasticsearch.ping.return_value = True
    mock_elasticsearch.indices.exists.return_value = True
    manager._initialize_es_index()
    mock_elasticsearch.indices.create.assert_not_called()

def test_chunks_exist_true(manager, mock_elasticsearch):
    mock_elasticsearch.search.return_value = {
        "hits": {
            "total": {"value": 1}
        }
    }
    result = manager.chunks_exist("prefix_test")
    assert result is True
    mock_elasticsearch.search.assert_called_with(
        index="test-index",
        body={"query": {"prefix": {"id": "prefix_test"}}},
        size=1
    )

def test_chunks_exist_false(manager, mock_elasticsearch):
    mock_elasticsearch.search.return_value = {
        "hits": {
            "total": {"value": 0}
        }
    }
    result = manager.chunks_exist("prefix_test")
    assert result is False
    mock_elasticsearch.search.assert_called_with(
        index="test-index",
        body={"query": {"prefix": {"id": "prefix_test"}}},
        size=1
    )

def test_chunks_exist_request_error(manager, mock_elasticsearch, caplog):

    mock_elasticsearch.search.side_effect = RequestError(
        400,
        ApiResponseMeta(400, "1.1", HttpHeaders(), 1, NodeConfig("http", "localhost", 1234)),
        {"error": "Error"}
    )
    result = manager.chunks_exist("prefix_test")
    assert result is False
    assert any("Error searching for chunks:" in message for message in caplog.messages)

def test_get_client_initializes_when_missing(manager, mock_elasticsearch):
    mock_elasticsearch.ping.return_value = True
    mock_elasticsearch.indices.exists_index_template.return_value = False
    mock_elasticsearch.indices.exists.return_value = False

    with patch.object(manager, "_create_index_template") as mock_template, \
         patch.object(manager, "_initialize_es_index") as mock_index:
        manager.get_client()
        mock_template.assert_called_once()
        mock_index.assert_called_once()

def test_get_client_doesnt_initialize_when_present(manager, mock_elasticsearch):
    mock_elasticsearch.ping.return_value = True
    mock_elasticsearch.indices.exists.return_value = True

    manager.get_client()

    # On the first go the code should verify the index template and index 
    # existence in the db:
    mock_elasticsearch.indices.exists.assert_called_once()
    mock_elasticsearch.indices.create.assert_not_called()
    mock_elasticsearch.put_index_template.assert_not_called()
    
    manager.get_client()
    
    # On the second go the result should be cached and we should not have made 
    # any more calls to the db to verify:
    mock_elasticsearch.indices.exists.assert_called_once()
    mock_elasticsearch.indices.create.assert_not_called()
    mock_elasticsearch.put_index_template.assert_not_called()
