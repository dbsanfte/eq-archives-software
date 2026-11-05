from array import array
from copy import deepcopy
import json
import math
import struct

import pytest

from indexer.vector_json import dumps,VECTOR_FIELDS


@pytest.mark.parametrize('field',sorted(VECTOR_FIELDS))
def test_dense_vectors_roundtrip_exact_float32_bits_without_changing_metadata(field):
    values=array('f',[-0.0,0.0,1.,-1.,.123456789012345,-.000000129381,1.e-35,3.e30])
    source={'text':[{'text_chunk':'日本語 and EverQuest','source_start':123,field:list(values)}],
            'score':.12345678901234567,'capture_date':'2001-01-01T00:00:00Z'}
    original=deepcopy(source);payload=dumps(source);after=json.loads(payload)
    assert array('f',after['text'][0][field]).tobytes()==values.tobytes()
    assert after['score']==source['score'] and after['capture_date']==source['capture_date']
    assert after['text'][0]['text_chunk']==source['text'][0]['text_chunk']
    assert source==original and struct.pack('f',source['text'][0][field][0])==struct.pack('f',-0.)
    assert len(payload)<len(json.dumps(source,ensure_ascii=False,separators=(',',':')).encode())


def test_tuples_arrays_empty_and_missing_vectors_keep_valid_json_shapes():
    source={'text':({'vector':array('f',[.1,.2])},{'vector':[]},{'vector':None}),
            'metadata':{'vector':'descriptive source text'},'values':(1,2)}
    after=json.loads(dumps(source))
    assert after['text'][0]['vector']==pytest.approx([.1,.2])
    assert after['text'][1:]==[{'vector':[]},{'vector':None}]
    assert after['metadata']==source['metadata'] and after['values']==[1,2]


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-float('inf'),1.e100,True])
def test_nonfinite_or_unrepresentable_vectors_fail_before_transport(value):
    with pytest.raises((ValueError,OverflowError)):
        dumps({'vector':[value]})


def test_nonfinite_metadata_is_rejected_instead_of_silently_changed_to_null():
    with pytest.raises(ValueError):dumps({'score':float('nan')})


def test_caller_default_remains_available_for_other_metadata():
    value=object()
    assert json.loads(dumps({'metadata':value},default=lambda v:'retained'))=={'metadata':'retained'}


def test_large_vector_payload_avoids_double_expansion_without_losing_elements():
    values=array('f',(math.sin(i)*.03 for i in range(768)))
    source={'text':[{'vector':list(values),'text_chunk':'guild history'} for _ in range(30)]}
    before=json.dumps(source,separators=(',',':')).encode();after=dumps(source)
    restored=json.loads(after)
    assert len(after)<len(before)*.7
    assert len(restored['text'])==30
    assert all(array('f',row['vector']).tobytes()==values.tobytes() for row in restored['text'])
