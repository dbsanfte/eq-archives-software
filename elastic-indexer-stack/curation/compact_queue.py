"""Stage-scoped SQL pagination; decode review manifests only on the visible page."""
from functools import lru_cache
import json

from common import CrawlError
from grading import current_rating
from portal import Stage, CANDIDATE_STAGES, REVIEW_STAGES
from capture_queue import QUEUE


@lru_cache(maxsize=65536)
def selected_grade(rating, captured):
    value=json.loads(rating) if rating else None
    return value.get('grade',-1) if captured and current_rating(value) else -1


def mapping(column, values, fallback):
    # All literals are fixed application enum/state constants, never caller SQL.
    return 'CASE '+column+' '+ ' '.join("WHEN '"+key+"' THEN '"+value.value+"'" for key,value in values.items())+' ELSE '+fallback+' END'


CANDIDATE=mapping('c.state',CANDIDATE_STAGES,"'candidates'")
STAGE="CASE WHEN json_extract(c.coverage,'$.capture.continuation') IS NOT NULL AND c.state IN ('approved_waiting_batch','capture_resume_queued','capturing') THEN "+CANDIDATE+' ELSE '+mapping('b.state',REVIEW_STAGES,CANDIDATE)+' END'
BASE="""WITH sites AS (SELECT c.id,c.url,c.scope,c.priority,c.state,c.coverage,c.captures,c.rating,
    """+STAGE+""" stage FROM candidates c INDEXED BY candidate_metadata LEFT JOIN batches b
    ON b.id=COALESCE(json_extract(c.coverage,'$.capture.review_id'),json_extract(c.coverage,'$.capture.batch_id')))
"""
TIER="COALESCE((SELECT MIN(COALESCE(NULLIF(json_extract(value,'$.tier'),0),3)) FROM json_each(s.captures)),3)"
ORIGINAL_SORT="(COALESCE(json_extract(s.rating,'$.grade'),-1)<2),"+TIER+",COALESCE(json_extract(s.rating,'$.grade'),-1) DESC,s.priority DESC,s.url"


def listing(store, request, captured_states):
    selected=request.query_params.get('filter','recommended')
    if selected not in ('recommended','pending','approved','captured','review','all',*Stage):
        raise CrawlError('Invalid queue filter')
    try:
        offset=int(request.query_params.get('offset','0'))
        if offset<0:raise ValueError()
    except ValueError:raise CrawlError('Invalid queue offset') from None
    search=request.query_params.get('search','').strip()
    if len(search)>200:raise CrawlError('Site search exceeds 200 characters')
    minimum=request.query_params.get('min_grade');needs=request.query_params.get('needs_grade','0')
    if selected=='candidates' and (minimum is not None and minimum not in ('0','1','2','3') or needs not in ('0','1')):
        raise CrawlError('Invalid candidate grade filter')
    db=store.db
    db.create_function('curation_grade',2,selected_grade,deterministic=True)
    db.create_function('curation_fold',1,lambda s:(s or '').casefold(),deterministic=True)
    stats={stage.value:0 for stage in Stage}
    for row in db.execute(BASE+'SELECT stage,COUNT(*) count FROM sites GROUP BY stage'):stats[row['stage']]=row['count']
    grades={str(n):0 for n in range(-1,4)}
    for row in db.execute(BASE+"SELECT curation_grade(rating,COALESCE(json_array_length(captures),0)>0) grade,COUNT(*) count FROM sites WHERE stage='candidates' GROUP BY grade"):
        grades[str(row['grade'])]=row['count']
    captured="state IN ("+','.join("'"+s+"'" for s in captured_states)+')'
    counters=dict(db.execute(BASE+"SELECT COALESCE(SUM(state='capturing'),0) capturing,COALESCE(SUM("+captured+"),0) captured,COALESCE(SUM(state IN ('approval_pending','deferred') AND json_extract(rating,'$.grade')>=2),0) recommended FROM sites").fetchone())
    condition='1';parameters=[]
    if selected in ('approved','queued'):condition="s.stage='queued'"
    elif selected=='recommended':condition="s.state IN ('approval_pending','deferred') AND json_extract(s.rating,'$.grade')>=2"
    elif selected=='pending':condition="s.state IN ('approval_pending','deferred')"
    elif selected=='captured':condition=captured.replace('state','s.state')
    elif selected=='review':condition="s.id IN (SELECT c.id FROM candidates c JOIN batches b ON b.id=COALESCE(json_extract(c.coverage,'$.capture.review_id'),json_extract(c.coverage,'$.capture.batch_id')) WHERE b.state='awaiting_review')"
    elif selected!='all':condition='s.stage=?';parameters.append(selected)
    if selected=='candidates':
        grade='curation_grade(s.rating,COALESCE(json_array_length(s.captures),0)>0)'
        if needs=='1':condition+=' AND '+grade+'<0'
        elif minimum is not None:condition+=' AND '+grade+'>=?';parameters.append(int(minimum))
    if search:
        condition+=" AND (instr(curation_fold(s.url||' '||s.scope||' '||COALESCE(json_extract(s.rating,'$.category'),'')),?)>0 OR EXISTS(SELECT 1 FROM json_each(s.captures) WHERE instr(curation_fold(json_extract(value,'$.title')),?)>0))"
        parameters.extend([search.casefold()]*2)
    total=db.execute(BASE+'SELECT COUNT(*) FROM sites s WHERE '+condition,parameters).fetchone()[0]
    order=ORIGINAL_SORT;join=''
    if selected in ('approved','queued'):
        join=' JOIN ('+QUEUE+') q ON q.id=s.id';order='q.held,q.ready,q.ordinal'
    elif selected=='candidates':order='curation_grade(s.rating,COALESCE(json_array_length(s.captures),0)>0) DESC,'+TIER+',s.priority DESC,s.url'
    elif selected in ('captured','review','indexing','history'):order="COALESCE(json_extract(s.coverage,'$.capture.completed_at'),'') DESC,"+ORIGINAL_SORT
    ids=[] if offset>=total else [row['id'] for row in db.execute(BASE+'SELECT s.id FROM sites s'+join+' WHERE '+condition+' ORDER BY '+order+' LIMIT 50 OFFSET ?',[*parameters,offset])]
    return {'ids':ids,'total':total,'offset':offset,'stage_counts':stats,'candidate_grades':grades,
            'all_count':sum(stats.values()),'approved':stats['queued'],**counters,
            'dismissal':{'count':stats['candidates'],'token':None},'review_actions':None}
