"""Compact JSON for dense vectors, preserving their exact float32 values.

Elasticsearch stores float vectors as float32. Expanding each value to Python's
double-precision decimal spelling can push otherwise valid documents over the
HTTP size limit. Nine significant digits round-trip every finite float32.
Other fields retain their normal JSON precision, and input documents stay intact.
"""
from array import array
import math

import orjson

VECTOR_FIELDS=frozenset(('vector','image_vector','llm_summary_vector','llm_image_text_vector'))


def prepare(value):
    if isinstance(value,dict):
        result={}
        for key,item in value.items():
            if key in VECTOR_FIELDS and (isinstance(item,(list,tuple,array,memoryview))
                                         or hasattr(item,'__array_interface__')):
                if hasattr(item,'__array_interface__'):
                    view=memoryview(item)
                    if view.ndim!=1 or view.format=='?':
                        raise ValueError('Dense vectors must be one-dimensional numeric arrays')
                if any(isinstance(number,bool) for number in item):
                    raise ValueError('Dense vectors must contain numbers, not booleans')
                values=array('f',item)
                if not all(math.isfinite(number) for number in values):
                    raise ValueError('Dense vectors must contain finite float32 values')
                numbers=(format(number,'.9g') if number!=0 or math.copysign(1,number)>0 else '-0.0'
                         for number in values)
                result[key]=orjson.Fragment('['+','.join(numbers)+']')
            else:
                result[key]=prepare(item)
        return result
    if isinstance(value,(list,tuple)):
        return [prepare(item) for item in value]
    if isinstance(value,float) and not math.isfinite(value):
        raise ValueError('JSON metadata must contain finite numbers')
    return value


def dumps(value,*,default=None):
    return orjson.dumps(prepare(value),default=default)
