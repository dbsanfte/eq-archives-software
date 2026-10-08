"""Real browser regressions for the mobile site workflow; isolated sources only."""
import argparse
import asyncio
import copy
import json
from urllib.parse import parse_qs,urlsplit

from playwright.async_api import async_playwright


async def settled(page):
    await page.wait_for_function('()=>!busy && data!==null')


async def stage(page,name):
    if name in ('candidates','queued','capturing','review','indexing'):
        await page.locator(f'#stages [data-view="{name}"]').click()
    else:
        await page.get_by_role('button',name='Open tools and history').click()
        await page.locator(f'#tools [data-view="{name}"]').click()
    await page.wait_for_function('(name)=>route.view===name && !route.candidate',arg=name)
    await page.locator('#candidates .site-tile, #candidates .empty').first.wait_for()


async def open_site(page,name):
    await page.get_by_role('button',name='Open '+name,exact=True).click()
    await page.locator('#site-workspace h1').filter(has_text=name).wait_for()
    await settled(page)


async def safe_layout(page,width):
    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
    nav=await page.locator('#stages').bounding_box()
    dock=await page.locator('#action-dock').bounding_box()
    if width<1000 and dock:
        assert dock['y']+dock['height']<=nav['y']+1,(width,nav,dock)
    for item in await page.locator('#stages button').all():
        box=await item.bounding_box()
        assert box['width']>=44 and box['height']>=44,(width,box)


async def basic_flow(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page()
    errors=[];external=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:external.append(request.url) if not request.url.startswith(base) else None)
    await page.goto(base)
    await page.locator('.site-tile').nth(2).wait_for()
    assert await page.locator('#stages button').count()==5
    assert await page.locator('#site-workspace').is_hidden()
    await safe_layout(page,width)
    await open_site(page,'guild.example/eq/')
    assert await page.locator('#stage-list').is_hidden()
    # Next site visits the current list rather than bouncing between two entries.
    seen={'guild.example/eq/'}
    for _ in range(2):
        await page.get_by_role('button',name='Next site →',exact=True).click()
        await page.wait_for_function('(seen)=>{const h=document.querySelector("#site-workspace h1");return h && !seen.includes(h.textContent)}',arg=list(seen))
        seen.add(await page.locator('#site-workspace h1').inner_text())
    assert len(seen)==3
    await page.get_by_role('button',name='Next site →',exact=True).click()
    await page.get_by_role('heading',name='guild.example/eq/',exact=True).wait_for()
    await page.get_by_role('button',name='Read source evidence · 2 captures').click()
    await page.get_by_role('button',name='Read guild EQ archive',exact=True).click()
    source=page.locator('.document-text')
    await source.filter(has_text='Early EverQuest guild history.').wait_for()
    assert 'Final paragraph.' in await source.inner_text()
    assert await page.evaluate('window.pwned') is None
    if width<1000:
        assert await page.locator('.page-browser').is_hidden()
        assert await source.evaluate('(node)=>getComputedStyle(node).overflowY')=='visible'
    async def delayed_source(route):
        if parse_qs(urlsplit(route.request.url).query).get('slot')==['0']: await asyncio.sleep(.5)
        await route.continue_()
    await page.route('**/api/source?**',delayed_source)
    # Clear only the disposable browser cache to exercise real response races.
    await page.evaluate('sourceCache.clear()')
    await page.get_by_label('Capture version').select_option('1')
    await page.get_by_label('Capture version').select_option('0')
    await page.get_by_label('Capture version').select_option('1')
    await source.filter(has_text='Later EverQuest guild history.').wait_for()
    await page.wait_for_timeout(650)
    assert 'Later EverQuest guild history.' in await source.inner_text()
    bookmark=page.url
    await page.reload()
    await source.filter(has_text='Later EverQuest guild history.').wait_for()
    assert page.url==bookmark
    assert await page.get_by_label('Capture version').input_value()=='1'
    await page.get_by_role('button',name='← Back to pages',exact=True).click()
    await page.locator('.page-browser').wait_for()
    if width<1000: assert await page.locator('.document-reader').is_hidden()
    await page.get_by_role('button',name='Back to site decision',exact=True).click()
    await page.get_by_role('heading',name='Choose capture scope',exact=True).wait_for()
    scope=page.get_by_label('Download scope',exact=True)
    await scope.select_option('custom')
    path=page.get_by_label('Custom capture folder path')
    await path.fill('/research')
    approve=page.get_by_role('button',name='Approve site for capture',exact=True)
    assert await approve.is_disabled()
    await page.locator('#refresh').click()
    await settled(page)
    assert await path.input_value()=='/research'
    # A five-second status poll must not replace this form or its draft.
    await page.wait_for_timeout(5200)
    assert await path.input_value()=='/research' and await approve.is_disabled()
    await page.get_by_role('button',name='Save capture scope',exact=True).click()
    await page.locator('.scope-value').filter(has_text='/research/').wait_for()
    await settled(page)
    assert await approve.is_enabled()
    await page.reload()
    await page.get_by_role('heading',name='Choose capture scope',exact=True).wait_for()
    assert await path.input_value()=='/research/'
    await approve.click()
    await settled(page)
    assert 'view=candidates' in page.url and 'candidate=' not in page.url
    assert await page.locator('#stage-list').is_visible()
    assert await page.locator('#site-workspace').is_hidden()
    assert not await page.get_by_role('button',name='Open guild.example/research/',exact=True).count()
    await page.locator('#notice').filter(has_text='guild.example/research/').wait_for()
    assert await page.locator('#notice').get_by_role('button',name='Undo approval',exact=True).is_visible()
    assert await page.locator('[data-count=queued]').inner_text()=='1'
    assert await page.locator('[data-count=candidates]').inner_text()=='2'
    await safe_layout(page,width)
    # Undo during an in-flight refresh remains a real action, never a dropped click.
    loading,release=asyncio.Event(),asyncio.Event()
    async def held_listing(route):
        response=await route.fetch();loading.set();await release.wait()
        try: await route.fulfill(response=response)
        except Exception: pass  # Superseded status requests can be aborted.
    await page.route('**/api/queue?**',held_listing)
    await page.locator('#refresh').click()
    await asyncio.wait_for(loading.wait(),timeout=3)
    await page.get_by_role('button',name='Undo approval',exact=True).click()
    release.set()
    await page.get_by_role('button',name='Open guild.example/research/',exact=True).wait_for()
    await page.unroute('**/api/queue?**',held_listing)
    assert 'view=candidates' in page.url and 'candidate=' not in page.url
    await open_site(page,'guild.example/research/')
    await page.get_by_role('button',name='Save for later',exact=True).click()
    await page.get_by_role('button',name='Restore to Candidates',exact=True).wait_for()
    assert 'view=saved' in page.url
    await page.get_by_role('button',name='Restore to Candidates',exact=True).click()
    await page.get_by_role('heading',name='Choose capture scope',exact=True).wait_for()
    await page.get_by_role('button',name='Dismiss site',exact=True).click()
    await page.get_by_role('button',name='Restore to Candidates',exact=True).wait_for()
    assert 'view=history' in page.url
    await page.get_by_role('button',name='Restore to Candidates',exact=True).click()
    await page.get_by_role('heading',name='Choose capture scope',exact=True).wait_for()
    # Restore the original scope so each width starts from identical durable data.
    await page.get_by_label('Download scope',exact=True).select_option('directory')
    await page.get_by_role('button',name='Save capture scope',exact=True).click()
    await settled(page)
    await page.screenshot(path=f'/tmp/curation-mobile-candidate-{width}.png',full_page=True)
    await stage(page,'review')
    await open_site(page,'captured-guild.example/eq/')
    await page.get_by_role('button',name='Browse 2 captured pages',exact=True).click()
    await page.get_by_label('Find a captured page').fill('child')
    assert await page.locator('.site-page').count()==1
    await page.get_by_role('button',name='Read Guild child guide',exact=True).click()
    await source.filter(has_text='Last child paragraph.').wait_for()
    links=await page.locator('.site-citation').get_attribute('href')
    assert links=='https://web.archive.org/web/20000101000000/http://captured-guild.example/eq/guide.html'
    assert not await page.get_by_role('button',name='Approve site & index',exact=True).count()
    await page.go_back()
    await page.locator('.page-browser').wait_for()
    assert await page.get_by_label('Find a captured page').input_value()=='child'
    await page.get_by_role('button',name='Back to site decision',exact=True).click()
    await page.get_by_role('button',name='Decline indexing',exact=True).click()
    await page.get_by_role('button',name='Reconsider indexing',exact=True).wait_for()
    assert 'view=history' in page.url
    await page.get_by_role('button',name='Reconsider indexing',exact=True).click()
    await page.get_by_role('button',name='Approve site & index',exact=True).wait_for()
    assert 'view=review' in page.url
    await page.screenshot(path=f'/tmp/curation-mobile-review-{width}.png',full_page=True)
    await safe_layout(page,width)
    await page.get_by_role('button',name='Open tools and history').click()
    bounds=await page.locator('#tools').bounding_box()
    assert bounds['x']>=0 and bounds['x']+bounds['width']<=width
    await page.get_by_role('button',name='Close tools',exact=True).click()
    assert not errors,errors
    assert not external,external
    await context.close()


async def long_reader(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page()
    listing=await (await context.request.get(base+'/api/queue?filter=review')).json()
    row=listing['candidates'][0]
    detail=await (await context.request.get(base+'/api/candidate?id='+row['id'])).json()
    template=detail['review']['manifest']['captures'][0]
    captures=[{**template,'url':f'http://long.example/page-{index+1}.html','title':f'Page {index+1}'} for index in range(31)]
    detail['review']['manifest']['captures']=captures
    detail['review']['source_slots']=list(range(31))
    detail['review']['page_identities']=[c['url'] for c in captures]
    detail['review']['job']={'name':'isolated-reader-fixture'}
    source_calls=[]
    async def fixture(route):
        assert route.request.method=='GET'
        path=urlsplit(route.request.url)
        if path.path=='/api/candidate':
            await asyncio.sleep(.4)  # Cached navigation must not reset scrolling when this arrives.
            body=detail
        elif path.path=='/api/source':
            index=int(parse_qs(path.query)['slot'][0]);source_calls.append(index)
            body={'complete_extracted_text':f'Source {index+1}\n'+('Original EverQuest text and preserved whitespace.\n'*160)+f'Last line {index+1}.'}
        elif path.path=='/api/queue':body=listing
        else:raise AssertionError(path.path)
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+f"/?view=review&candidate={row['id']}&screen=pages")
    await page.locator('.site-page').nth(30).wait_for(state='attached')
    if width<1000:
        assert await page.locator('.site-pages').evaluate('(n)=>getComputedStyle(n).overflowY')=='visible'
        assert await page.locator('.document-reader').is_hidden()
        await page.evaluate('window.scrollTo(0,1200)')
        before=await page.evaluate('scrollY')
    else:
        await page.get_by_role('button',name='Scroll pages down',exact=True).click()
        await page.wait_for_function("()=>document.querySelector('.site-pages').scrollTop>0")
        await page.locator('.site-pages').focus();await page.keyboard.press('End')
    last_page=page.get_by_role('button',name='Read Page 31',exact=True)
    await last_page.scroll_into_view_if_needed()
    list_position=await page.evaluate("[scrollY,document.querySelector('.site-pages').scrollTop]")
    await last_page.click()
    await page.locator('.document-text').filter(has_text='Last line 31.').wait_for()
    assert await page.get_by_role('button',name='Next page',exact=True).is_disabled()
    await page.get_by_role('button',name='Previous page',exact=True).click()
    await page.locator('.document-text').filter(has_text='Last line 30.').wait_for()
    await page.get_by_role('button',name='Next page',exact=True).click()
    await page.locator('.document-text').filter(has_text='Last line 31.').wait_for()
    # Measure scrolling after the navigation frame has painted and restored its position.
    await page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    if width<1000: await page.evaluate('window.scrollTo(0,1400)')
    else: await page.get_by_role('button',name='Scroll document down',exact=True).click()
    coordinates=await page.evaluate("[scrollY,document.querySelector('.document-text').scrollTop]")
    fetched=len(source_calls)
    # Background polling and manual refresh leave the open reader DOM intact.
    await page.wait_for_timeout(5200)
    actual=await page.evaluate("[scrollY,document.querySelector('.document-text').scrollTop]")
    assert actual==coordinates,(width,coordinates,actual)
    assert len(source_calls)==fetched
    if width==390:
        # Another operator can complete a site while this reader remains open.
        detail['review']['state']='indexed'
        detail['candidate'].update(stage='history',state='indexed',review_state='indexed')
        await page.wait_for_url('**view=history**',timeout=8000)
        await page.locator('.site-header .eyebrow').filter(has_text='History').wait_for()
        await page.locator('.document-text').filter(has_text='Last line 31.').wait_for()
        await page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
        assert await page.evaluate("[scrollY,document.querySelector('.document-text').scrollTop]")==coordinates
        assert len(source_calls)==fetched
    async with page.expect_response(lambda response:'/api/candidate?' in response.url):
        await page.locator('#refresh').click()
    await page.wait_for_function('()=>!busy')
    assert 'Last line 31.' in await page.locator('.document-text').inner_text()
    await safe_layout(page,width)
    await page.screenshot(path=f'/tmp/curation-mobile-long-reader-{width}.png',full_page=True)
    await page.go_back()
    await page.locator('.page-browser').wait_for()
    if width<1000: await page.wait_for_function('(expected)=>scrollY===expected[0]',arg=list_position)
    assert await page.locator('.site-page').count()==31
    await context.close()


async def status_flow(browser,base):
    context=await browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page=await context.new_page()
    listing=await (await context.request.get(base+'/api/queue?filter=review')).json()
    row=next(row for row in listing['candidates'] if 'captured-guild' in row['url'])
    detail=await (await context.request.get(base+'/api/candidate?id='+row['id'])).json()
    # Exercise the real manifest-bound site approval once; the fixture has no worker.
    await page.goto(base+'/?view=review&candidate='+row['id'])
    await page.get_by_role('button',name='Approve site & index',exact=True).click()
    await page.get_by_role('heading',name='Publishing approved files',exact=True).wait_for()
    assert 'view=indexing' in page.url
    detail=await (await context.request.get(base+'/api/candidate?id='+row['id'])).json()
    approved=copy.deepcopy(detail)
    mutations=[]
    async def fixture(route):
        parsed=urlsplit(route.request.url)
        if route.request.method=='POST':
            mutations.append((parsed.path,route.request.post_data_json))
            if parsed.path=='/api/resume': detail['review']['operation']['state']='queued';detail['review']['operation']['error']=None
            elif parsed.path=='/api/index-retry':
                assert route.request.post_data_json=={'id':detail['review']['id'],'manifest_sha256':detail['review']['manifest_sha256'],'job_name':'failed-import'}
                detail['review']['state']='published_waiting_index';detail['candidate']['review_state']='published_waiting_index'
                detail['review']['error']=None
            else:raise AssertionError(parsed.path)
            await route.fulfill(status=202,json={'accepted':True});return
        if parsed.path=='/api/candidate': body=detail
        elif parsed.path=='/api/queue':
            body=copy.deepcopy(listing);view=parse_qs(parsed.query)['filter'][0]
            body['candidates']=[detail['candidate']] if view==detail['candidate']['stage'] else []
            body['total']=len(body['candidates']);body['operations']=[]
            body['stage_counts']={name:int(name==detail['candidate']['stage']) for name in ('candidates','queued','capturing','review','indexing','saved','history')}
        else:await route.continue_();return
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    detail['review']['operation']['state']='interrupted';detail['review']['operation']['error']='Publication transport failed; sources retained'
    await page.locator('#refresh').click()
    await page.get_by_role('heading',name='Publication paused',exact=True).wait_for()
    await page.get_by_role('button',name='Retry publication',exact=True).click()
    await page.get_by_role('heading',name='Publishing approved files',exact=True).wait_for()
    detail['review']['state']='index_failed';detail['candidate']['review_state']='index_failed';detail['review']['job']={'name':'failed-import'}
    detail['review']['error']='AI enrichment stopped after two correction attempts. Luna date evidence is not a verbatim source excerpt.'
    await page.locator('#refresh').click()
    await page.get_by_role('heading',name='Indexing needs attention',exact=True).wait_for()
    await page.get_by_text(detail['review']['error'],exact=True).wait_for()
    await page.get_by_role('button',name='Retry indexing',exact=True).click()
    await page.get_by_role('heading',name='Waiting to index',exact=True).wait_for()
    assert not await page.get_by_text('AI enrichment stopped after two correction attempts.',exact=False).count()
    detail['review']['state']='indexing';detail['candidate']['review_state']='indexing'
    await page.locator('#refresh').click()
    await page.get_by_role('heading',name='AI enrichment & indexing',exact=True).wait_for()
    await page.get_by_text('Indexing details',exact=True).click()
    await page.wait_for_timeout(5200)
    assert await page.locator('#stage-live details').evaluate('(n)=>n.open')
    detail['review']['state']='indexed';detail['candidate'].update(stage='history',state='indexed',review_state='indexed')
    await page.locator('#refresh').click()
    await page.get_by_role('heading',name='Indexed · complete',exact=True).wait_for()
    assert 'view=history' in page.url
    assert await page.locator('[data-count=indexing]').inner_text()=='0'
    assert not await page.locator('#stages [aria-current]').count()
    assert not await page.get_by_role('button',name='Approve site & index',exact=True).count()
    assert detail['review']['manifest']==approved['review']['manifest']
    assert [path for path,_ in mutations]==['/api/resume','/api/index-retry']
    await stage(page,'indexing')
    assert await page.locator('.site-tile').count()==0
    await stage(page,'history')
    await page.locator('.site-tile').wait_for()
    await context.close()


async def search_and_queue(browser,base):
    context=await browser.new_context(viewport={'width':320,'height':844})
    page=await context.new_page()
    snapshot=await (await context.request.get(base+'/api/queue?filter=all')).json()
    template=snapshot['candidates'][0]
    remaining=61
    async def listing(route):
        args=parse_qs(urlsplit(route.request.url).query)
        offset=int(args['offset'][0]);query=args.get('search',[''])[0]
        rows=[{**template,'id':f'{index:024x}','url':f'http://site-{index:02}.example/','scope':f'http://site-{index:02}.example/','stage':'queued','state':'approved_waiting_batch','review_state':None} for index in range(remaining)]
        if query:rows=[row for row in rows if query in row['scope']]
        body={**snapshot,'candidates':rows[offset:offset+50],'total':len(rows),'approved':remaining,'stage_counts':{'candidates':0,'queued':remaining,'capturing':0,'review':0,'indexing':0,'saved':0,'history':0}}
        if query=='site-1':await asyncio.sleep(.6)
        await route.fulfill(status=200,json=body)
    await page.route('**/api/queue?**',listing)
    await page.goto(base+'/?view=queued')
    await page.locator('#page').filter(has_text='1–50 of 61').wait_for()
    await page.locator('#next').click()
    await page.locator('#page').filter(has_text='51–61 of 61').wait_for()
    remaining=47
    await page.locator('#view-total').filter(has_text='47 sites').wait_for(timeout=8000)
    assert await page.locator('.site-tile').count()==47 and await page.locator('#pagination').is_hidden()
    search=page.get_by_label('Find a site',exact=True)
    await search.fill('site-1');await page.wait_for_timeout(250);await search.fill('site-2')
    await page.locator('#view-total').filter(has_text='10 sites').wait_for()
    await page.wait_for_timeout(700)
    assert await search.input_value()=='site-2'
    assert all('site-2' in name for name in await page.locator('.site-tile h2').all_text_contents())
    assert await page.locator('[data-count=queued]').inner_text()=='47'
    await context.close()


async def unavailable_candidate(browser,base):
    context=await browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page=await context.new_page()
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    row=copy.deepcopy(snapshot['candidates'][0])
    row.update(state='unavailable',stage='candidates',captures=[],rating=None,error='No usable Wayback capture was found.')
    async def fixture(route):
        parsed=urlsplit(route.request.url)
        if route.request.method=='POST':
            assert parsed.path=='/api/decisions'
            assert route.request.post_data_json==[{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'reject'}]
            row.update(state='rejected',stage='history')
            await route.fulfill(status=200,json={'saved':1});return
        if parsed.path=='/api/candidate':body={'candidate':row,'review':None}
        elif parsed.path=='/api/queue':
            view=parse_qs(parsed.query)['filter'][0]
            body={**snapshot,'candidates':[row] if view==row['stage'] else [],'total':int(view==row['stage']),
                  'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','review','indexing','saved','history')}}
        else:raise AssertionError(parsed.path)
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates&candidate='+row['id'])
    await page.get_by_text('No usable Wayback capture was found.',exact=True).wait_for()
    assert await page.get_by_role('button',name='Approve site for capture',exact=True).is_disabled()
    assert await page.get_by_role('button',name='Save for later',exact=True).is_enabled()
    await page.get_by_role('button',name='Dismiss site',exact=True).click()
    await page.get_by_role('button',name='Restore to Candidates',exact=True).wait_for()
    assert 'view=history' in page.url
    await context.close()


async def coverage_feedback(browser,base,width):
    for outcome in ('new_site','already_archived'):
        context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
        page=await context.new_page()
        snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
        row=copy.deepcopy(snapshot['candidates'][0])
        row.update(state='coverage_unverified',stage='candidates',scope_mode='page',
                   url='http://pub6.ezboard.com/bthemagicianstower.html',scope='http://pub6.ezboard.com/bthemagicianstower.html')
        row['coverage']={'status':'inventory_partial','complete':False,'site_check':{'status':'inventory_partial','complete':False}}
        attempts=0
        async def fixture(route):
            nonlocal attempts
            parsed=urlsplit(route.request.url)
            if route.request.method=='POST':
                assert parsed.path=='/api/coverage'
                assert route.request.post_data_json=={'id':row['id'],'manifest_sha256':row['manifest_sha256']}
                attempts+=1
                if attempts==1:
                    check={'status':'inventory_partial','complete':False,'reason':'tree_limit','retryable':True,
                           'message':'The metadata read limit was reached. Continue from the saved position.',
                           'progress':{'host':'pub6.ezboard.com','checked':4095,'total':63764}}
                elif attempts==2:
                    check={**row['coverage']['site_check'],'reason':'metadata_unavailable','retryable':False,
                           'message':'Archive metadata is unavailable locally. An operator must restore it before approval; no automatic fetch was attempted.'}
                else:
                    check={'status':outcome,'complete':True,'archive_sha':'1'*40}
                    row.update(state='already_archived' if outcome=='already_archived' else 'approval_pending',
                               stage='history' if outcome=='already_archived' else 'candidates')
                    if outcome=='already_archived':check['archive_path']='websites/pub6.ezboard.com/20000101000000/bthemagicianstower.html'
                row['coverage']['site_check']=check
                await route.fulfill(status=200,json={'checked':row['id'],'state':row['state'],'coverage':check});return
            if parsed.path=='/api/candidate':body={'candidate':row,'review':None}
            elif parsed.path=='/api/queue':
                view=parse_qs(parsed.query)['filter'][0]
                body={**snapshot,'candidates':[row] if view==row['stage'] else [],'total':int(view==row['stage']),
                      'operations':[],'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','review','indexing','saved','history')}}
            else:raise AssertionError(parsed.path)
            await route.fulfill(status=200,json=body)
        await page.route('**/api/**',fixture)
        await page.goto(base+'/?view=candidates&candidate='+row['id'])
        approve=page.get_by_role('button',name='Approve site for capture',exact=True)
        await page.get_by_role('button',name='Recheck archive coverage',exact=True).click()
        await page.locator('#notice').filter(has_text='Coverage is still unverified.').wait_for()
        assert await page.locator('#notice').get_attribute('data-tone')=='attention'
        assert await approve.is_disabled()
        await page.get_by_text('4,095 of 63,764 archive snapshots checked on pub6.ezboard.com.',exact=True).wait_for()
        await page.get_by_role('button',name='Continue coverage check',exact=True).click()
        await page.locator('#notice').filter(has_text='An operator must restore it').wait_for()
        assert await approve.is_disabled()
        await page.get_by_role('button',name='Recheck archive coverage',exact=True).click()
        await page.locator('#notice').filter(has_text='Coverage verified:').wait_for()
        assert await page.locator('#notice').get_attribute('data-tone')==''
        assert not await page.get_by_text('Coverage is not yet verified. Capture approval remains blocked.',exact=True).count()
        if outcome=='new_site':
            assert await approve.is_enabled()
            await page.get_by_text('No archived copy of this site/account was found.',exact=True).wait_for()
        else:
            assert 'view=history' in page.url and not await approve.count()
            await page.get_by_text('This site/account is already represented in the archive.',exact=True).wait_for()
            assert await page.get_by_role('link',name='View existing archived capture').get_attribute('href')=='https://github.com/dbsanfte/eq-archives/tree/'+'1'*40+'/websites/pub6.ezboard.com/20000101000000/bthemagicianstower.html'
        assert attempts==3
        await safe_layout(page,width)
        if width==390:await page.screenshot(path=f'/tmp/curation-coverage-{outcome}-{width}.png',full_page=True)
        await context.close()


async def capture_flow(browser,base):
    context=await browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page=await context.new_page()
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    row=copy.deepcopy(snapshot['candidates'][0])
    row.update(stage='queued',state='approved_waiting_batch',decision={'capture_after':'2030-01-01T00:00:00Z'})
    context_data={'candidate':row,'review':None,'queue_position':2,'queue_blocker':None,'capture_queue_error':None,'capture_operation':None}
    async def fixture(route):
        parsed=urlsplit(route.request.url)
        if route.request.method=='POST':
            if parsed.path=='/api/undo':
                row.update(stage='capturing',state='capturing')
                context_data['capture_operation']={'id':'f'*32,'state':'running','kind':'capture','payload':{},'result':{'progress':{'phase':'downloading','files':3,'bytes':500,'urls_checked':4,'sites_total':1,'site_url':row['scope'],'current_url':row['url']}}}
                await route.fulfill(status=409,json={'error':'Capture has already started; approval can no longer be undone.'});return
            assert parsed.path=='/api/resume' and route.request.post_data_json=={'id':'f'*32}
            context_data['capture_operation']['state']='running'
            await route.fulfill(status=202,json={'resumed':'f'*32});return
        if parsed.path=='/api/candidate': body=context_data
        elif parsed.path=='/api/queue':
            view=parse_qs(parsed.query)['filter'][0]
            body={**snapshot,'candidates':[row] if view==row['stage'] else [],'total':int(view==row['stage']),
                'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','review','indexing','saved','history')},
                'operations':[context_data['capture_operation']] if context_data['capture_operation'] else []}
        else:await route.continue_();return
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+f"/?view=queued&candidate={row['id']}")
    await page.get_by_role('heading',name='Ready for automatic capture',exact=True).wait_for()
    await page.get_by_role('button',name='Undo approval',exact=True).click()
    await page.locator('#error').filter(has_text='Capture has already started').wait_for()
    await page.get_by_role('heading',name='Capture in progress',exact=True).wait_for(timeout=8000)
    assert not await page.get_by_role('button',name='Undo approval',exact=True).count()
    op=context_data['capture_operation'];op['state']='interrupted';op['error']='Wayback budget pause'
    await page.locator('#refresh').click()
    await page.get_by_role('heading',name='Capture paused',exact=True).wait_for()
    await page.get_by_role('button',name='Resume capture',exact=True).click()
    await page.get_by_role('heading',name='Capture in progress',exact=True).wait_for()
    op['result']['progress']['files']=9
    await page.locator('#stage-live').filter(has_text='9 HTML files staged').wait_for(timeout=8000)
    # A completed download changes only this selected site's workspace and stage.
    completed_list=await (await context.request.get(base+'/api/queue?filter=review')).json()
    completed=completed_list['candidates'][0]
    completed_context=await (await context.request.get(base+'/api/candidate?id='+completed['id'])).json()
    row.update(stage='review',state='captured_awaiting_review')
    context_data['review']=completed_context['review']
    op['state']='completed'
    await page.locator('#refresh').click()
    await page.get_by_role('heading',name='Your captured site',exact=True).wait_for()
    assert 'view=review' in page.url
    assert await page.locator('[data-count=capturing]').inner_text()=='0'
    assert await page.locator('[data-count=review]').inner_text()=='1'
    assert await page.get_by_role('button',name='Approve site & index',exact=True).is_enabled()
    await context.close()


async def discovery_flow(browser,base,width):
    """Every paid action is intercepted; this test cannot run a discovery worker."""
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page()
    errors=[];posts=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    operations=[{'id':'a'*32,'kind':'publish','state':'running','payload':{}}]
    loading,release_listing=asyncio.Event(),asyncio.Event()
    submitted,release_action=asyncio.Event(),asyncio.Event()
    reject=False
    async def fixture(route):
        nonlocal operations
        path=urlsplit(route.request.url).path
        if route.request.method=='POST':
            posts.append((path,route.request.post_data_json))
            assert path in ('/api/discover','/api/resume'),path
            if reject:
                operations=[{'id':'c'*32,'kind':'publish','state':'running','payload':{}}]
                await route.fulfill(status=409,json={'error':'Archive publication has already started.'});return
            submitted.set();await release_action.wait()
            if path=='/api/discover':
                assert route.request.post_data_json=={'max_candidates':50,'max_usd':2}
                operations=[{'id':'b'*32,'kind':'discover','state':'queued','payload':route.request.post_data_json}]
            else:
                assert route.request.post_data_json=={'id':'b'*32}
                operations[0]['state']='queued'
            await route.fulfill(status=202,json={'operation':'b'*32});return
        assert path=='/api/queue',path
        loading.set();await release_listing.wait()
        await route.fulfill(status=200,json={'candidates':[],'total':0,'offset':0,'operations':operations,
            'stage_counts':{name:0 for name in ('candidates','queued','capturing','review','indexing','saved','history')},
            'capture_queue_error':None,'version':'discovery-fixture'})
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates')
    await asyncio.wait_for(loading.wait(),timeout=3)
    button=page.locator('#discover');status=page.locator('#discovery-status')
    assert await page.locator('#stage-list #discover').is_visible(),'Discovery must be on Candidates, outside More'
    assert await button.is_disabled(),'Availability must be checked before enabling a paid action'
    release_listing.set();await settled(page)
    await status.filter(has_text='Archive publication is running.').wait_for()
    assert await button.is_disabled() and not posts
    await page.locator('#manual-url').fill('http://typed-while-busy.example/')
    assert await page.locator('#manual-submit').is_disabled()
    assert 'discovery-status' in await button.get_attribute('aria-describedby')
    assert '50 candidates' in await page.locator('#discovery-limits').inner_text()
    assert '$2' in await page.locator('#discovery-limits').inner_text()
    await safe_layout(page,width)
    bounds=await button.bounding_box()
    assert bounds['height']>=48 and bounds['width']>=44
    assert bounds['y']>=0 and bounds['y']+bounds['height']<min(760,await page.evaluate('innerHeight'))
    await page.screenshot(path=f'/tmp/curation-discovery-blocked-{width}.png',full_page=True)
    await page.get_by_role('button',name='Open tools and history').click()
    assert not await page.locator('#tools #discover').count()
    await page.get_by_role('button',name='Close tools',exact=True).click()
    for name in ('capturing','queued','review','indexing'):
        await stage(page,name)
        assert await button.is_hidden(),name
    await stage(page,'candidates')
    assert await button.is_visible()
    # Each worker type explains the block, including queued publication.
    for kind,state,text in (('publish','queued','Archive publication is queued.'),('capture','running','Capture is running.'),('discover','running','Discovery is running.')):
        operations=[{'id':'a'*32,'kind':kind,'state':state,'payload':{'max_candidates':50,'max_usd':2}}]
        await page.locator('#refresh').click()
        await status.filter(has_text=text).wait_for()
        assert await button.is_disabled()
    operations=[]
    # The ordinary poll releases the button; it must never start a paid run itself.
    await page.wait_for_function('()=>!document.getElementById("discover").disabled',timeout=8000)
    assert not posts
    assert await page.locator('#manual-url').input_value()=='http://typed-while-busy.example/'
    await page.screenshot(path=f'/tmp/curation-discovery-ready-{width}.png',full_page=True)
    await button.click();await asyncio.wait_for(submitted.wait(),timeout=3)
    assert await button.is_disabled()
    await status.filter(has_text='Submitting your request').wait_for()
    bounds=await button.bounding_box()
    await page.mouse.click(bounds['x']+bounds['width']/2,bounds['y']+bounds['height']/2)
    assert len(posts)==1,'Rapid repeat taps must not submit a second paid run'
    release_action.set()
    await status.filter(has_text='Discovery is queued.').wait_for();await settled(page)
    assert await button.is_disabled()
    await page.reload();await settled(page)
    assert await button.is_disabled() and len(posts)==1
    # Resume is also on Candidates and retains the existing run's smaller budget.
    operations[0].update(state='interrupted',payload={'max_candidates':12,'max_usd':0.4})
    await page.locator('#refresh').click()
    await page.get_by_role('button',name='Resume discovery',exact=True).filter(visible=True).wait_for()
    assert await button.inner_text()=='Resume discovery'
    assert '12 candidates' in await page.locator('#discovery-limits').inner_text()
    assert '$0.4' in await page.locator('#discovery-limits').inner_text()
    await status.filter(has_text='original limits').wait_for()
    await button.click()
    await status.filter(has_text='Discovery is queued.').wait_for();await settled(page)
    assert posts[-1]==('/api/resume',{'id':'b'*32})
    assert operations[0]['payload']=={'max_candidates':12,'max_usd':0.4}
    operations=[];reject=True
    await page.reload();await settled(page)
    assert await button.is_enabled()
    await button.click()
    await page.locator('#error').filter(has_text='Archive publication has already started.').wait_for()
    # A worker claim between polling and clicking refreshes the visible blocker.
    await status.filter(has_text='Archive publication is running.').wait_for()
    assert await button.is_disabled() and await page.locator('#notice').is_hidden()
    assert len(posts)==3 and not errors,(posts,errors)
    await context.close()


async def manual_site_flow(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page()
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    row=copy.deepcopy(snapshot['candidates'][0])
    submitted='https://web.archive.org/web/20000101000000/'+row['url']
    row['evidence']=[{'kind':'manual_submission','source_url':submitted,'original_url':row['url']}]
    operations=[];rows=[];posts=[];errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    received,release=asyncio.Event(),asyncio.Event()
    async def fixture(route):
        nonlocal operations
        path=urlsplit(route.request.url).path
        if route.request.method=='POST':
            assert path=='/api/submit-site'
            payload=route.request.post_data_json;posts.append(payload)
            assert payload['max_usd']==2
            received.set();await release.wait()
            if len(posts)==1:
                operations=[{'id':'d'*32,'kind':'discover','state':'queued','payload':{'max_candidates':1,'max_usd':2,
                    'target':{'url':row['url'],'submitted_url':payload['url']}}}]
                await route.fulfill(status=202,json={'operation':'d'*32,'url':row['url']})
            elif len(posts)==2:
                await route.fulfill(status=200,json={'candidate_id':row['id'],'url':row['url'],'existing':True})
            else:await route.fulfill(status=409,json={'error':'Enter a public HTTP(S) website URL or a Wayback link to its original page'})
            return
        if path=='/api/queue':
            await route.fulfill(status=200,json={**snapshot,'operations':operations,'candidates':rows,'total':len(rows),
                'stage_counts':{**snapshot['stage_counts'],'candidates':len(rows)}})
        elif path=='/api/candidate':await route.fulfill(status=200,json={'candidate':row,'review':None})
        else:await route.continue_()
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates');await settled(page)
    field=page.get_by_role('textbox',name='Website or Wayback URL',exact=True)
    button=page.get_by_role('button',name='Add & grade site',exact=True)
    assert await button.is_disabled()
    await field.fill(submitted)
    assert await button.is_enabled()
    await page.locator('#refresh').click();await settled(page)
    assert await field.input_value()==submitted and not posts
    await field.scroll_into_view_if_needed()
    await safe_layout(page,width)
    await page.screenshot(path=f'/tmp/curation-manual-site-{width}.png',full_page=True)
    await field.press('Enter');await asyncio.wait_for(received.wait(),timeout=3)
    assert await button.is_disabled() and await page.locator('#discover').is_disabled()
    # An edit typed while the first submission is in flight is never erased.
    await field.fill('http://next.example/')
    release.set()
    await page.locator('#discovery-status').filter(has_text='Site check is queued.').wait_for();await settled(page)
    assert await field.input_value()=='http://next.example/'
    assert posts==[{'url':submitted,'max_usd':2}]
    await page.locator('#manual-result').filter(has_text=row['url']).wait_for()
    operations[0].update(state='completed',result={'candidate_id':row['id'],'candidates':1})
    rows.append(row)
    await page.locator('#manual-result').get_by_role('link',name='Open site',exact=True).wait_for(timeout=8000)
    assert await page.locator('#candidates .site-tile').count()==1
    await page.locator('#manual-result').get_by_role('link',name='Open site',exact=True).click()
    await page.get_by_text('Luna grade 3/3 · guild',exact=True).wait_for()
    assert await page.locator('#manual-site').is_hidden()
    await page.get_by_text('1 discovery references',exact=True).click()
    assert await page.get_by_role('link',name=submitted,exact=True).get_attribute('href')==submitted
    await stage(page,'candidates')
    await field.fill(row['url']);await button.click()
    await page.locator('#manual-result').filter(has_text='already in the portal').wait_for();await settled(page)
    assert await page.locator('#candidates .site-tile').count()==1
    await field.fill('javascript:alert(1)');await button.click()
    await page.locator('#error').filter(has_text='Enter a public HTTP(S)').wait_for()
    assert await field.input_value()=='javascript:alert(1)'
    assert len(posts)==3 and not errors,(posts,errors)
    await context.close()


async def capture_approval_navigation(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page()
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    template=snapshot['candidates'][0]
    rows=[{**copy.deepcopy(template),'id':f'{index+1:024x}','url':f'http://fixture-{index:02}.example/',
           'scope':f'http://fixture-{index:02}.example/','stage':'candidates','state':'approval_pending'} for index in range(67)]
    requests=[];posts=[];errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    submitted,release=asyncio.Event(),asyncio.Event()
    async def fixture(route):
        path=urlsplit(route.request.url).path
        args=parse_qs(urlsplit(route.request.url).query)
        if route.request.method=='POST':
            payload=route.request.post_data_json;posts.append((path,payload))
            selected=payload[0] if path=='/api/decisions' else payload
            row=next(row for row in rows if row['id']==selected['id'])
            assert selected['manifest_sha256']==row['manifest_sha256']
            if path=='/api/decisions':
                assert selected['decision']=='approve'
                if len(posts)==1:
                    await route.fulfill(status=409,json={'error':'Approval could not be saved. Try again.'});return
                submitted.set();await release.wait()
                row.update(stage='queued',state='approved_waiting_batch',decision={'capture_after':'2030-01-01T00:00:00Z'})
            else:
                assert path=='/api/undo'
                row.update(stage='candidates',state='approval_pending',decision=None)
            await route.fulfill(status=200,json={'saved':1});return
        if path=='/api/queue':
            requests.append(args)
            candidates=[row for row in rows if row['stage']==args['filter'][0] and args.get('search',[''])[0] in row['scope']]
            offset=int(args['offset'][0])
            await route.fulfill(status=200,json={**snapshot,'operations':[],'candidates':candidates[offset:offset+50],
                'total':len(candidates),'offset':offset,'stage_counts':{name:sum(row['stage']==name for row in rows)
                    for name in ('candidates','queued','capturing','review','indexing','saved','history')}})
        elif path=='/api/candidate':
            await route.fulfill(status=200,json={'candidate':next(row for row in rows if row['id']==args['id'][0]),'review':None})
        else:raise AssertionError(path)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates&offset=50&search=fixture-')
    await page.locator('#page').filter(has_text='51–67 of 67').wait_for()
    await page.locator('[data-candidate="'+rows[50]['id']+'"]').scroll_into_view_if_needed()
    position=await page.evaluate('scrollY')
    await open_site(page,'fixture-50.example')
    approve=page.get_by_role('button',name='Approve site for capture',exact=True)
    await approve.click()
    await page.locator('#error').filter(has_text='Approval could not be saved.').wait_for()
    assert await page.locator('#site-workspace').is_visible()
    assert 'candidate='+rows[50]['id'] in page.url and rows[50]['stage']=='candidates'
    await approve.click();await asyncio.wait_for(submitted.wait(),timeout=3)
    assert await approve.is_disabled() and 'candidate='+rows[50]['id'] in page.url
    release.set()
    await page.wait_for_function('()=>!busy && route.view==="candidates" && !route.candidate')
    await page.locator('#page').filter(has_text='51–66 of 66').wait_for()
    assert 'offset=50' in page.url and 'search=fixture-' in page.url
    assert await page.get_by_label('Find a site',exact=True).input_value()=='fixture-'
    assert not await page.locator('[data-candidate="'+rows[50]['id']+'"]').count()
    await page.wait_for_function('(position)=>Math.abs(scrollY-position)<2',arg=position)
    assert await page.locator('[data-count=queued]').inner_text()=='1'
    assert requests[-1]['filter']==['candidates'] and requests[-1]['offset']==['50']
    await safe_layout(page,width)
    # Undo from the confirmation affects the approved site and stays on this list.
    await page.locator('#notice').get_by_role('button',name='Undo approval',exact=True).click()
    await page.locator('#page').filter(has_text='51–67 of 67').wait_for()
    assert posts[-1][0]=='/api/undo' and posts[-1][1]['id']==rows[50]['id']
    assert 'candidate=' not in page.url and await page.locator('#site-workspace').is_hidden()
    # Reloaded success stays on Candidates; Undo is still available from Queue.
    await open_site(page,'fixture-50.example');await approve.click()
    await page.wait_for_function('()=>!busy && !route.candidate')
    await page.reload();await settled(page)
    assert 'view=candidates' in page.url and 'candidate=' not in page.url
    await stage(page,'queued');await open_site(page,'fixture-50.example')
    await page.get_by_role('button',name='Undo approval',exact=True).click()
    await page.get_by_role('heading',name='Choose capture scope',exact=True).wait_for()
    assert rows[50]['stage']=='candidates' and not errors,errors
    await context.close()


async def candidate_quick_actions(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<1000,has_touch=width<1000)
    page=await context.new_page()
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    rows=[{**copy.deepcopy(snapshot['candidates'][0]),'id':f'{index+101:024x}',
           'url':f'http://swipe-{index}.example/eq/news.html','scope':f'http://swipe-{index}.example/eq/',
           'scope_mode':'directory','stage':'candidates','state':'approval_pending'} for index in range(12)]
    rows[1]['state']='coverage_unverified'
    spend={'complete':True,'today':{'estimated_usd':.1234,'unresolved_usd':.02},
           'month':{'estimated_usd':4.5672,'unresolved_usd':.03}}
    posts=[];errors=[];fail_approval=True
    submitted,release=asyncio.Event(),asyncio.Event()
    queue_release=asyncio.Event();queue_release.set()
    page.on('pageerror',lambda error:errors.append(str(error)))
    async def fixture(route):
        nonlocal fail_approval
        path=urlsplit(route.request.url).path
        args=parse_qs(urlsplit(route.request.url).query)
        if route.request.method=='POST':
            payload=route.request.post_data_json;posts.append((path,payload))
            decision=payload[0] if path=='/api/decisions' else payload
            row=next(row for row in rows if row['id']==decision['id'])
            assert decision['manifest_sha256']==row['manifest_sha256']
            if path=='/api/decisions':
                if decision['decision']=='approve':
                    if fail_approval:
                        fail_approval=False
                        await route.fulfill(status=409,json={'error':'Coverage changed; review this site again.'});return
                    submitted.set();await release.wait()
                    row.update(stage='queued',state='approved_waiting_batch')
                else:
                    assert decision['decision']=='reject'
                    row.update(stage='history',state='rejected')
            else:
                assert path in ('/api/undo','/api/restore')
                row.update(stage='candidates',state='approval_pending')
            await route.fulfill(status=200,json={'saved':1});return
        if path=='/api/queue':
            await queue_release.wait()
            selected=[row for row in rows if row['stage']==args['filter'][0]]
            await route.fulfill(status=200,json={**snapshot,'operations':[],'luna_spend':spend,'candidates':selected,
                'total':len(selected),'offset':0,'stage_counts':{name:sum(row['stage']==name for row in rows)
                    for name in ('candidates','queued','capturing','review','indexing','saved','history')}})
        elif path=='/api/candidate':
            await route.fulfill(status=200,json={'candidate':next(row for row in rows if row['id']==args['id'][0]),'review':None})
        else:raise AssertionError(path)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates&search=swipe-');await settled(page)
    assert await page.locator('#spend-today').inner_text()=='$0.123'
    assert await page.locator('#spend-month').inner_text()=='$4.567'
    await page.get_by_label('Luna spend details',exact=True).click()
    assert await page.locator('#spend-status').is_visible()
    assert 'UTC calendar' in await page.locator('#spend-status').inner_text()
    assert 'today $0.020' in await page.locator('#spend-status').inner_text()
    await safe_layout(page,width)
    box=await page.locator('#spend-status').bounding_box()
    assert box['x']>=0 and box['x']+box['width']<=width
    spend['today']['estimated_usd']=.246
    await page.locator('#refresh').click()
    await page.locator('#spend-today').filter(has_text='$0.246').wait_for()
    assert await page.locator('#luna-spend').get_attribute('open') is not None
    spend['complete']=False
    await page.locator('#refresh').click()
    await page.locator('#spend-status').filter(has_text='temporarily incomplete').wait_for()
    assert await page.locator('#spend-today').inner_text()=='—'
    spend['complete']=True
    await page.get_by_label('Luna spend details',exact=True).click()
    cdp=await context.new_cdp_session(page)
    def tile(index):return page.get_by_role('button',name=f'Open swipe-{index}.example/eq/',exact=True)
    async def swipe(index,dx,dy=0,cancel=False,reverse=False):
        item=tile(index)
        await item.evaluate('(el)=>window.scrollTo(0,el.getBoundingClientRect().top+scrollY-170)')
        await page.wait_for_timeout(60)
        box=await item.bounding_box()
        x,y=box['x']+box['width']/2,box['y']+65
        before_scroll=await page.evaluate('scrollY')
        await cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
        for step in range(1,7):
            await cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x+dx*step/6,'y':y+dy*step/6}]})
        if abs(dx)>=125 and not dy and not await page.evaluate('busy'):
            card=item.locator('..').locator('..')
            assert await card.get_attribute('data-direction')==('approve' if dx>0 else 'reject')
            if dx<0 or rows[index]['state']=='approval_pending' and not await page.evaluate('(id)=>drafts.get(id)?.dirty',rows[index]['id']):
                await page.wait_for_function('(id)=>document.querySelector(`[data-candidate="${id}"]`).closest(".candidate-card").classList.contains("swipe-ready")',arg=rows[index]['id'],timeout=2000)
                assert 'swipe-ready' in await card.get_attribute('class')
                assert await card.locator('.reject-cue' if dx<0 else '.approve-cue').inner_text()==('Release to dismiss' if dx<0 else 'Release to approve')
        if reverse:
            await cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y}]})
        await cdp.send('Input.dispatchTouchEvent',{'type':'touchCancel' if cancel else 'touchEnd','touchPoints':[]})
        await page.wait_for_timeout(220)
        if dy:assert abs(await page.evaluate('scrollY')-before_scroll)>10
    if width<1000:
        # Real Chromium touch scrolling and cancelled/short/reversed drags must
        # neither decide nor accidentally open the site under the finger.
        await swipe(0,0,-80)
        await swipe(0,25)
        await swipe(0,125,cancel=True)
        await swipe(0,-125,reverse=True)
        assert not posts and 'candidate=' not in page.url
        await swipe(1,125)
        assert not posts and await page.get_by_role('button',name='Approve capture: swipe-1.example/eq/',exact=True).is_disabled()
        await tile(0).tap();await page.get_by_role('heading',name='Choose capture scope',exact=True).wait_for()
    else:await open_site(page,'swipe-0.example/eq/')
    # Drafts survive a return to the list; quick approval cannot bypass Save.
    await page.get_by_label('Download scope',exact=True).select_option('custom')
    await page.get_by_label('Custom capture folder path').fill('/unsaved')
    queue_release.clear()
    await page.locator('#stages [data-view=candidates]').click()
    await page.locator('#candidates').get_by_text('Loading sites…',exact=True).wait_for()
    assert not await page.get_by_role('button',name='Approve capture: swipe-0.example/eq/',exact=True).count()
    queue_release.set()
    await tile(0).wait_for();await settled(page)
    assert await page.get_by_role('button',name='Approve capture: swipe-0.example/eq/',exact=True).is_disabled()
    if width<1000:await swipe(0,125)
    assert not posts
    await page.reload();await settled(page)
    async def decide(index,approve):
        if width<1000:await swipe(index,125 if approve else -125)
        else:await page.get_by_role('button',name=f'{"Approve capture" if approve else "Dismiss"}: swipe-{index}.example/eq/',exact=True).press('Enter')
    await decide(0,False);await settled(page)
    assert not await tile(0).count() and rows[0]['stage']=='history'
    assert 'candidate=' not in page.url and 'view=candidates' in page.url
    undo=page.locator('#notice').get_by_role('button',name='Undo dismissal',exact=True)
    assert await undo.is_visible()
    await undo.click();await tile(0).wait_for();await settled(page)
    assert posts[-1][0]=='/api/restore' and rows[0]['stage']=='candidates'
    await decide(0,True)
    await page.locator('#error').filter(has_text='Coverage changed').wait_for()
    assert await tile(0).count() and rows[0]['stage']=='candidates'
    assert 'candidate=' not in page.url
    await decide(0,True);await asyncio.wait_for(submitted.wait(),timeout=3)
    count=len(posts)
    assert await page.get_by_role('button',name='Approve capture: swipe-0.example/eq/',exact=True).is_disabled()
    if width<1000:await swipe(2,125)
    assert len(posts)==count and rows[0]['stage']=='candidates'
    release.set();await page.wait_for_function('()=>!busy && data.stage_counts.queued===1')
    assert not await tile(0).count() and rows[0]['stage']=='queued'
    undo=page.locator('#notice').get_by_role('button',name='Undo approval',exact=True)
    assert await undo.is_visible()
    await safe_layout(page,width)
    await undo.click();await tile(0).wait_for();await settled(page)
    assert posts[-1][0]=='/api/undo' and posts[-1][1]['id']==rows[0]['id']
    assert rows[0]['stage']=='candidates' and 'candidate=' not in page.url
    if width<1000:
        item=tile(0);await item.scroll_into_view_if_needed()
        box=await item.bounding_box();x,y=box['x']+box['width']/2,box['y']+65
        count=len(posts)
        await cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'id':0,'x':x,'y':y}]})
        await cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'id':0,'x':x+40,'y':y}]})
        await cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'id':0,'x':x+40,'y':y},{'id':1,'x':x-30,'y':y+30}]})
        await cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
        assert len(posts)==count and not await page.locator('.candidate-card.dragging').count()
        # A metadata poll during a swipe cancels the obsolete gesture, instead
        # of submitting an approval for evidence which has just changed.
        item=tile(0);await item.scroll_into_view_if_needed()
        box=await item.bounding_box();x,y=box['x']+box['width']/2,box['y']+65
        await cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
        await cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x+125,'y':y}]})
        count=len(posts);rows[0]['manifest_sha256']='f'*64
        await page.evaluate('refresh()')
        await cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
        assert len(posts)==count
    assert not errors,errors
    await context.close()


async def check(base):
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch()
        try:
            for width in (320,390,430,768,1280):
                await candidate_quick_actions(browser,base,width)
                await capture_approval_navigation(browser,base,width)
                await discovery_flow(browser,base,width)
                await manual_site_flow(browser,base,width)
                await basic_flow(browser,base,width)
                await long_reader(browser,base,width)
                await coverage_feedback(browser,base,width)
                print(f'Mobile workflow and complete-source navigation verified at {width}px',flush=True)
            await search_and_queue(browser,base)
            await unavailable_candidate(browser,base)
            await capture_flow(browser,base)
            await status_flow(browser,base)
            print('Queue pagination, source availability, capture races, retries and retirement verified',flush=True)
        finally:await browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('url');args=parser.parse_args()
    asyncio.run(check(args.url.rstrip('/')))
    print('Mobile workflow browser checks passed at 320/390/430/768/1280 px: stage transitions, Undo, drafts, full readers, dated sources, Back, polling, retries, retirement and unbounded paginated queues')
