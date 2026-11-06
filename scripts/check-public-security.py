#!/usr/bin/env python3
"""Bounded public regressions: rejected work must not become database/model calls."""
import json
import sys
import urllib.error
import urllib.request


def check(base):
    def call(path, body=None):
        req=urllib.request.Request(base.rstrip('/')+path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={'Content-Type':'application/json'})
        try:
            response=urllib.request.urlopen(req,timeout=15)
        except urllib.error.HTTPError as error:response=error
        with response:
            raw=response.read(1024*1024)
            return response.status,raw
    path='/elasticsearch/eq-archive/_search'
    for body in ({'size':1,'script_fields':{'audit':{'script':'return 1;'}}}, {'runtime_mappings':{}},
                 {'size':10000}, {'query':{'script':{'script':'return true;'}}},
                 {'aggs':{'unsafe':{'terms':{'field':'domain_name','size':65536}}}}):
        assert call(path,body)[0]==400,'Unsafe query was not rejected'
    assert call(path+'?scroll=10s',{'size':0})[0]==400,'Scroll contexts remain public'
    assert call(path+'/unexpected',{'size':0})[0]==404,'Search path is not exact'
    model='embeddinggemma-2-768-v1'
    assert call('/openai/v1/embeddings',{'model':model,'input':['task: search result | query: a']*2})[0]==400
    assert call('/openai/v1/embeddings',{'model':model,'input':['task: search result | query: '+'x'*601]})[0]==400
    status,raw=call(path,{'size':0,'query':{'match_all':{}}})
    assert status==200 and 'hits' in json.loads(raw),'Valid search unavailable after rejected requests'
    print('Public script, scroll, aggregation, response-size, path and embedding admission regressions passed.')


if __name__=='__main__':check(sys.argv[1])
