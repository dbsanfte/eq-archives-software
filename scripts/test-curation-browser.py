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
    await page.get_by_role('heading',name='Ready for automatic capture',exact=True).wait_for()
    assert 'view=queued' in page.url
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
    await page.get_by_role('heading',name='Choose capture scope',exact=True).wait_for()
    await page.unroute('**/api/queue?**',held_listing)
    assert 'view=candidates' in page.url
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
    await page.locator('#refresh').click()
    await page.get_by_role('heading',name='Indexing needs attention',exact=True).wait_for()
    await page.get_by_role('button',name='Retry indexing',exact=True).click()
    await page.get_by_role('heading',name='Waiting to index',exact=True).wait_for()
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


async def check(base):
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch()
        try:
            for width in (320,390,430,768,1280):
                await basic_flow(browser,base,width)
                await long_reader(browser,base,width)
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
