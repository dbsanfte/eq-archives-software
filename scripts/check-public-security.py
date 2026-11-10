#!/usr/bin/env python3
"""Bounded public regressions: rejected work must not become database/model calls."""
import json
import argparse
import urllib.error
import urllib.request


def check(base, mcp_base=None):
    def call(path, body=None, headers=None, endpoint=None):
        req=urllib.request.Request((endpoint or base).rstrip('/')+path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={'Content-Type':'application/json',**(headers or {})})
        try:
            response=urllib.request.urlopen(req,timeout=15)
        except urllib.error.HTTPError as error:response=error
        with response:
            raw=response.read(1024*1024)
            return response.status,raw
    path='/elasticsearch/eq-archive/_search'
    vector=[0]*768
    vector[0]=10**1000
    url_phrase={'match_phrase':{'url':'a '*500}}
    for body in ({'size':1,'script_fields':{'audit':{'script':'return 1;'}}}, {'runtime_mappings':{}},
                 {'size':10000}, {'query':{'script':{'script':'return true;'}}},
                 {'aggs':{'unsafe':{'terms':{'field':'domain_name','size':65536}}}},
                 {'size':1,'query':{'ids':{'values':['security-probe-missing-id']}},'_source':[]},
                 {'size':0,'knn':{'field':'text.vector','k':1,'num_candidates':1,'query_vector':vector}},
                 {'size':0,'query':{'bool':{'should':[{'bool':{'should':[url_phrase]*20}}]*4}}},
                 {'size':0,'query':{'multi_match':{'query':'a/'*101,
                     'fields':['text_full','llm_summary','llm_image_text'],'type':'best_fields'}}}):
        assert call(path,body)[0]==400,'Unsafe query was not rejected'
    assert call(path+'?scroll=10s',{'size':0})[0]==400,'Scroll contexts remain public'
    assert call(path+'/unexpected',{'size':0})[0]==404,'Search path is not exact'
    model='embeddinggemma-2-768-v1'
    assert call('/openai/v1/embeddings',{'model':model,'input':['task: search result | query: a']*2})[0]==400
    assert call('/openai/v1/embeddings',{'model':model,'input':['task: search result | query: '+'x'*601]})[0]==400
    assert call('/openai/v1/embeddings',{'model':model,'input':'task: search result | query: \ud800'})[0]==400
    status,raw=call(path,{'size':0,'query':{'match_all':{}}})
    assert status==200 and 'hits' in json.loads(raw),'Valid search unavailable after rejected requests'
    rpc_headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'}
    for tool in ('search','search_archive','list_sources','list_sites'):
        status,raw=call('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':tool,'arguments':{'query':'a/'*101}}},rpc_headers,mcp_base)
        assert status==200 and json.loads(raw)['result']['isError'],'MCP query bypassed analyzed-text limits'
    status,raw=call('/mcp',{'jsonrpc':'2.0','id':1,'method':'subscriptions/listen','params':{
        '_meta':{'io.modelcontextprotocol/protocolVersion':'2026-07-28',
                 'io.modelcontextprotocol/clientCapabilities':{}},'notifications':{'toolsListChanged':True}}},
        {**rpc_headers,'MCP-Protocol-Version':'2026-07-28','Mcp-Method':'subscriptions/listen'},mcp_base)
    assert status==400 and json.loads(raw)['error']['code']==-32601,'MCP subscription streams remain public'
    status,raw=call('/mcp',{'jsonrpc':'2.0','id':1,'method':'ping'},rpc_headers,mcp_base)
    assert status==200 and 'result' in json.loads(raw),'MCP unavailable after rejected requests'
    print('Public DSL, projection, query-cost, numeric, Unicode, MCP method and admission regressions passed.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base')
    parser.add_argument('--mcp-base',help='Separate MCP base URL for isolated frontend/API containers')
    args=parser.parse_args()
    check(args.base,args.mcp_base)
