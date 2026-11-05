from array import array
from datetime import datetime,timezone
from decimal import Decimal
import json

import pytest
from elastic_transport import SerializationError

from indexer.es_serialization import serializers
from indexer.es_manager import ElasticsearchManager


@pytest.mark.parametrize('mimetype',[
    'application/json','application/vnd.elasticsearch+json',
    'application/x-ndjson','application/vnd.elasticsearch+x-ndjson'])
def test_all_elasticsearch_wire_routes_preserve_vectors_and_standard_metadata(mimetype):
    serializer=serializers()[mimetype];values=array('f',[.123456789, -.987654321])
    document={'text':[{'vector':list(values)}],'date':datetime(2001,1,1,tzinfo=timezone.utc),'price':Decimal('1.125')}
    ndjson='ndjson' in mimetype;payload=serializer.dumps([document] if ndjson else document)
    row=json.loads(payload)
    assert array('f',row['text'][0]['vector']).tobytes()==values.tobytes()
    assert row['date']=='2001-01-01T00:00:00+00:00' and row['price']==1.125
    assert payload.endswith(b'\n')==ndjson
    if ndjson:assert serializer.loads(payload)==[row]
    else:assert serializer.loads(payload)==row


def test_invalid_vector_is_a_serialization_failure_before_any_request():
    with pytest.raises(SerializationError):serializers()['application/json'].dumps({'vector':[float('nan')]})


def test_manager_installs_vector_serializers_on_real_client_configuration(monkeypatch):
    from unittest.mock import Mock
    client=Mock();client.ping.return_value=True
    factory=Mock(return_value=client);monkeypatch.setattr('indexer.es_manager.Elasticsearch',factory)
    manager=ElasticsearchManager(es_username_file='/missing',es_password_file='/missing',manage_index=False)
    assert manager.get_client() is client
    assert set(factory.call_args.kwargs['serializers'])==set(serializers())


@pytest.mark.parametrize('mimetype',['application/json','application/vnd.elasticsearch+json',
                                   'application/x-ndjson','application/vnd.elasticsearch+x-ndjson'])
def test_real_indexer_numpy_outputs_use_compact_float32_path(mimetype):
    import numpy as np
    values=np.random.default_rng(1).normal(size=768).astype(np.float32);values[0]=-0.
    document={'text':[{'vector':values,'text_chunk':'source bound passage'}]}
    serializer=serializers()[mimetype];payload=serializer.dumps([document] if 'ndjson' in mimetype else document)
    saved=json.loads(payload)
    assert np.asarray(saved['text'][0]['vector'],dtype=np.float32).tobytes()==values.tobytes()
    expanded=json.dumps({'text':[{'vector':values.tolist(),'text_chunk':'source bound passage'}]}).encode()
    assert len(payload)<len(expanded)*.7


@pytest.mark.parametrize('kind',['boolean','nested','nan'])
def test_invalid_numpy_vectors_are_rejected_before_transport(kind):
    import numpy as np
    value={'boolean':np.ones(768,dtype=np.bool_),'nested':np.ones((2,384),dtype=np.float32),
           'nan':np.array([np.nan],dtype=np.float32)}[kind]
    with pytest.raises(SerializationError):serializers()['application/json'].dumps({'vector':value})
