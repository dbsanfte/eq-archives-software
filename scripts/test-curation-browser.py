"""Real Chromium checks against the built intranet service with isolated sources."""

import argparse
import asyncio
import json
import re

from playwright.async_api import async_playwright


async def view(page, name):
    """Use visible navigation; the extra inventory views live in Tools & help."""
    if name in ('recommended','approved','capturing','captured'):
        await page.locator(f'.views [data-view="{name}"]').click()
    else:
        if not await page.locator('#tools').evaluate('(node)=>node.open'):
            await page.locator('#tools > summary').click()
        bounds=await page.locator('.tools-menu').bounding_box()
        assert bounds['x']>=0 and bounds['x']+bounds['width']<=await page.evaluate('innerWidth'),bounds
        await page.locator('#filter').select_option(name)


async def check_review_navigation(browser, base):
    """Long site/page lists and a long document remain distinct and independently usable."""
    for width in (320,390,768,1280,1440):
        context=await browser.new_context(viewport={'width':width,'height':1000})
        page=await context.new_page()
        response=await context.request.get(base+'/api/queue?filter=captured')
        queue=await response.json()
        candidate=queue['candidates'][0]
        review_id=candidate['coverage']['capture']['review_id']
        response=await context.request.get(f"{base}/api/site?id={review_id}&candidate={candidate['id']}")
        site=await response.json()
        template=site['manifest']['captures'][0]
        site['manifest']['captures']=[{**template,'title':f'Captured page {index+1}',
            'url':f'http://captured-guild.example/eq/page-{index+1}.html'} for index in range(21)]
        site['source_slots']=list(range(21))
        site['page_identities']=[capture['url'] for capture in site['manifest']['captures']]
        site['state']='awaiting_review'
        site['job']={'name':'isolated-navigation-fixture'}
        queue.update(total=12,all_count=12,captured=12,approved=0,capturing=0,operations=[],batches=[],awaiting_site_review=12)
        queue['candidates']=[{**candidate,'id':candidate['id'] if index==0 else f'{index:024x}',
            'scope':candidate['scope'] if index==0 else f'http://other-{index}.example/',
            'state':'captured_awaiting_review','review_state':'awaiting_review'} for index in range(12)]
        requests=[]
        async def fixture(route):
            from urllib.parse import parse_qs,urlsplit
            requests.append(route.request)
            assert route.request.method=='GET','Browsing must never approve or publish a site'
            parsed=urlsplit(route.request.url)
            if parsed.path=='/api/queue': body=queue
            elif parsed.path=='/api/site': body=site
            elif parsed.path=='/api/source':
                slot=int(parse_qs(parsed.query)['slot'][0])
                body={'complete_extracted_text':f'Complete page {slot+1}\n'+('An original EverQuest source paragraph.\n'*120)+f'Last line of page {slot+1}.'}
            else: raise AssertionError(parsed.path)
            await route.fulfill(status=200,json=body)
        await page.route('**/api/**',fixture)
        await page.goto(f"{base}/?view=captured&site={review_id}&candidate={candidate['id']}")
        await page.locator('.site-source pre').filter(has_text='Last line of page 1.').wait_for()
        await page.locator('#view-title').filter(has_text='Review & indexing').wait_for()
        assert await page.get_by_role('heading',name='Captured sites',exact=True).is_visible()
        assert await page.get_by_role('heading',name='Captured pages',exact=True).is_visible()
        assert await page.get_by_role('region',name='Document preview',exact=True).count()==1
        assert await page.locator('.page-navigation .browse-hint').inner_text()=='Scroll to browse pages ↓'
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
        await page.get_by_text('Indexing details',exact=True).click()
        # Check real layout/scroll gutters, not jsdom's unpainted CSS properties.
        geometry=await page.evaluate('''() => {
            const list=document.querySelector('.site-pages'),nav=list.parentElement,reader=document.querySelector('.site-source');
            const a=nav.getBoundingClientRect(),b=reader.getBoundingClientRect();
            return {gutter:list.offsetWidth-list.clientWidth,overflow:getComputedStyle(list).overflowY,
                nav:getComputedStyle(nav).backgroundColor,reader:getComputedStyle(reader).backgroundColor,
                gap:innerWidth<=600 ? b.top-a.bottom : b.left-a.right};
        }''')
        assert geometry['gutter']>=12 and geometry['overflow']=='scroll',geometry
        assert geometry['nav']!=geometry['reader'] and geometry['gap']>=14,geometry
        pages=page.locator('.site-pages')
        reader=page.locator('.site-source pre')
        await page.get_by_role('button',name='Scroll captured pages down',exact=True).click()
        await page.wait_for_function("document.querySelector('.site-pages').scrollTop>0")
        assert await reader.evaluate('(node)=>node.scrollTop')==0
        before=await pages.evaluate('(node)=>node.scrollTop')
        await pages.focus()
        await page.keyboard.press('PageDown')
        await page.wait_for_function('(previous)=>document.querySelector(".site-pages").scrollTop>previous',arg=before)
        await page.keyboard.press('End')
        await page.wait_for_function('''()=>{const n=document.querySelector('.site-pages');return n.scrollTop+n.clientHeight>=n.scrollHeight-2}''')
        await page.get_by_role('button',name='Captured page 21',exact=True).click()
        await reader.filter(has_text='Last line of page 21.').wait_for()
        assert await page.locator('.page-position').inner_text()=='Page 21 of 21'
        assert await page.get_by_role('button',name='Next page',exact=True).is_disabled()
        assert await page.locator('.site-citation a').get_attribute('href')==f"https://web.archive.org/web/{template['timestamp']}/http://captured-guild.example/eq/page-21.html"
        await page.get_by_role('button',name='Previous page',exact=True).click()
        await reader.filter(has_text='Last line of page 20.').wait_for()
        await page.get_by_role('button',name='Next page',exact=True).click()
        await reader.filter(has_text='Last line of page 21.').wait_for()
        await page.get_by_role('button',name='Scroll document text down',exact=True).click()
        await page.wait_for_function("document.querySelector('.site-source pre').scrollTop>0")
        await page.get_by_role('button',name='Scroll captured sites down',exact=True).click()
        positions=await page.evaluate("['#candidates','.site-pages','.site-source pre'].map(s=>document.querySelector(s).scrollTop)")
        source_requests=len([request for request in requests if '/api/source?' in request.url])
        # A real status poll updates the decision panel without resetting any reading pane.
        site['state']='indexing_declined'
        queue['candidates'][0]['review_state']='indexing_declined'
        await page.locator('.site-status .capture-phase').filter(has_text='Indexing declined').wait_for(timeout=8000)
        assert await page.locator('.technical-details').evaluate('(node)=>node.open')
        assert await page.evaluate("['#candidates','.site-pages','.site-source pre'].map(s=>document.querySelector(s).scrollTop)")==positions
        assert await page.locator('.site-page [aria-current=page]').inner_text()=='Captured page 21'
        assert len([request for request in requests if '/api/source?' in request.url])==source_requests
        assert 'Last line of page 21.' in await reader.inner_text()
        async with page.expect_response(lambda response:'/api/site?' in response.url):
            await page.locator('#refresh').click()
        await page.wait_for_function('!busy')
        assert await page.evaluate("['#candidates','.site-pages','.site-source pre'].map(s=>document.querySelector(s).scrollTop)")==positions
        assert await page.locator('.site-page [aria-current=page]').inner_text()=='Captured page 21'
        await page.screenshot(path=f'/tmp/eqarchives-curation-navigation-{width}.png',full_page=True)
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        await context.close()


async def check(base):
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch()
        try:
            await check_review_navigation(browser,base)
            for width in (320,390,1280):
                context=await browser.new_context(viewport={'width':width,'height':900})
                page=await context.new_page()
                external=[]
                page.on('request',lambda request: external.append(request.url) if not request.url.startswith(base) else None)
                await page.goto(base)
                await page.locator('#status').filter(has_text='5 candidates').wait_for()
                assert await page.locator('#candidates article').count()==2
                assert await page.locator('.views button').count()==4
                assert not await page.locator('#tools').evaluate('(node)=>node.open')
                assert await page.locator('.views [aria-current=page]').get_attribute('data-view')=='recommended'
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert await page.evaluate('window.pwned') is None
                await page.screenshot(path=f'/tmp/eqarchives-curation-suggestions-{width}.png',full_page=True)
                await view(page,'all')
                await page.locator('#status').filter(has_text='5 in this view').wait_for()
                assert await page.locator('#candidates article').count()==5
                card=page.locator('#candidates article').filter(has=page.get_by_role('link',name='http://guild.example/eq/news.html',exact=True))
                await card.locator('summary').click()
                await card.locator('pre').filter(has_text='Early EverQuest guild history.').wait_for()
                assert 'Final paragraph.' in await card.locator('pre').inner_text()
                async def delayed(route):
                    if 'slot=0' in route.request.url: await asyncio.sleep(.4)
                    await route.continue_()
                await page.route('**/api/source?**',delayed)
                await card.get_by_label('Source capture').select_option('1')
                await card.get_by_label('Source capture').select_option('0')
                await card.get_by_label('Source capture').select_option('1')
                await card.locator('pre').filter(has_text='Later EverQuest guild history.').wait_for()
                await page.wait_for_timeout(500)
                assert 'Later EverQuest guild history.' in await card.locator('pre').inner_text()
                assert await page.evaluate('window.pwned') is None
                await card.get_by_label('Capture scope').select_option('page')
                await card.locator('.scope').filter(has_text='exact page only').wait_for()
                await card.get_by_role('button',name='Approve for capture',exact=True).click()
                await page.locator('#status').filter(has_text='1 awaiting capture').wait_for()
                await page.locator('#next-step').filter(has_text='automatic capture').wait_for()
                await view(page,'recommended')
                await page.locator('#status').filter(has_text='1 in this view').wait_for()
                assert await page.locator('#candidates article').count()==1
                assert not await page.locator('#candidates').get_by_role('link',name='http://guild.example/eq/news.html',exact=True).count()
                await page.get_by_role('button',name='Awaiting capture',exact=True).click()
                await page.locator('#candidates').get_by_role('button',name='Undo approval',exact=True).wait_for()
                await page.reload()
                await page.locator('#status').filter(has_text='1 awaiting capture').wait_for()
                await page.get_by_role('button',name='Awaiting capture',exact=True).click()
                entered,release=asyncio.Event(),asyncio.Event()
                async def held_refresh(route):
                    response=await route.fetch()
                    if not entered.is_set():
                        entered.set()
                        await release.wait()
                    await route.fulfill(response=response)
                await page.route('**/api/queue?**',held_refresh)
                await page.locator('#refresh').click()
                await asyncio.wait_for(entered.wait(),timeout=3)
                await page.get_by_role('button',name='Undo approval',exact=True).click()
                release.set()
                await page.locator('#status').filter(has_text='0 awaiting capture').wait_for(timeout=8000)
                entered.clear()
                release.clear()
                await page.locator('#refresh').click()
                await asyncio.wait_for(entered.wait(),timeout=3)
                await view(page,'recommended')
                release.set()
                await page.locator('#status').filter(has_text='2 in this view').wait_for(timeout=8000)
                assert await page.locator('#candidates article').count()==2
                await page.unroute('**/api/queue?**',held_refresh)
                await view(page,'recommended')
                await page.locator('#status').filter(has_text='2 in this view').wait_for()
                await view(page,'all')
                card=page.locator('#candidates article').filter(has=page.get_by_role('link',name='http://guild.example/eq/news.html',exact=True))
                capture_files=2
                virtual_state=None
                async def active_operation(route):
                    response=await route.fetch()
                    body=await response.json()
                    body['operations']=[{'id':'b'*32,'kind':'capture','state':'completed' if virtual_state=='captured_awaiting_review' else 'running',
                        'payload':{'batch_id':'a'*32},'result':{'progress':{'phase':'ready_for_review' if virtual_state=='captured_awaiting_review' else 'downloading',
                        'files':capture_files,'bytes':1234,'urls_checked':1,'sites_done':0,'sites_total':1,'site_url':'http://guild.example/eq/',
                        'current_url':'http://guild.example/eq/guide.html'}}}]
                    if virtual_state=='capturing':
                        from urllib.parse import parse_qs,urlsplit
                        chosen=parse_qs(urlsplit(route.request.url).query)['filter'][0]
                        current={**virtual_row,'state':virtual_state,'coverage':{**virtual_row['coverage'],'capture':{'batch_id':'a'*32}}}
                        if chosen=='capturing':
                            body['candidates']=[current]
                            body['total']=1
                        body['capturing']=int(virtual_state=='capturing')
                    await route.fulfill(status=200,content_type='application/json',body=json.dumps(body))
                await page.route('**/api/queue?**',active_operation)
                await page.locator('#refresh').click()
                await page.locator('#activity h3').filter(has_text='Capturing sites').wait_for()
                await card.locator('summary').click()
                await card.locator('pre').filter(has_text='Early EverQuest guild history.').wait_for()
                source_text=await card.locator('pre').inner_text()
                capture_files=3
                await page.locator('#activity .capture-counts').filter(has_text='3 HTML files staged').wait_for(timeout=8000)
                assert await card.locator('pre').inner_text()==source_text
                await card.get_by_label('Capture scope',exact=True).select_option('custom')
                await card.get_by_label('Custom capture folder path').fill('/research')
                capture_files=4
                await page.locator('#activity .capture-counts').filter(has_text='4 HTML files staged').wait_for(timeout=8000)
                assert await card.get_by_label('Custom capture folder path').input_value()=='/research'
                assert not await card.get_by_role('button',name='Approve for capture',exact=True).is_enabled()
                await card.get_by_role('button',name='Save custom scope',exact=True).click()
                await card.locator('.scope').filter(has_text='http://guild.example/research/').wait_for()
                await page.reload()
                card=page.locator('#candidates article').filter(has=page.get_by_role('link',name='http://guild.example/eq/news.html',exact=True))
                assert await card.get_by_label('Capture scope',exact=True).input_value()=='custom'
                assert await card.get_by_label('Custom capture folder path').input_value()=='/research/'
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                # A background transition removes queued items from that view and
                # exposes completed downloads in Recent captures.
                snapshot=await context.request.get(base+'/api/queue?filter=all')
                virtual_row=next(row for row in (await snapshot.json())['candidates'] if row['url']=='http://guild.example/eq/news.html')
                virtual_state='capturing'
                await view(page,'capturing')
                await page.locator('#status').filter(has_text='1 capturing').wait_for()
                assert not await page.get_by_role('button',name='Undo approval',exact=True).count()
                virtual_state='captured_awaiting_review'
                await page.locator('#refresh').click()
                await page.locator('#next-step').filter(has_text='2 captured sites').wait_for()
                await page.locator('#capture-activity').wait_for(state='hidden')
                await page.get_by_role('button',name='Review & indexing',exact=True).click()
                await page.locator('#status').filter(has_text='2 in this view').wait_for()
                assert await page.locator('#filter').input_value()=='captured'
                assert await page.locator('#candidates article').count()==2
                assert not await page.get_by_role('button',name='Undo approval',exact=True).count()
                guild=page.locator('#candidates article').filter(has=page.get_by_role('link',name='http://captured-guild.example/eq/',exact=True))
                other=page.locator('#candidates article').filter(has=page.get_by_role('link',name='http://captured-class.example/eq/',exact=True))
                await guild.get_by_role('button',name='Review site',exact=True).click()
                pane=page.locator('#reviewed-site')
                await pane.locator('p').filter(has_text='2 pages · 3 dated captures').wait_for()
                assert await pane.locator('article').count()==1
                assert not await page.get_by_label('Include this capture').count()
                links=await pane.locator('.site-pages a').evaluate_all('(links)=>links.map(link=>link.href)')
                assert len(links)==3 and all('web.archive.org/web/' in link and 'captured-guild.example' in link for link in links)
                assert any('/web/20000101000000/http://captured-guild.example/eq/guide.html' in link for link in links)
                await pane.get_by_role('button',name='Guild child guide',exact=True).click()
                await pane.locator('pre').filter(has_text='Last child paragraph.').wait_for()
                # Updating unrelated acquisition progress must preserve the open page.
                virtual_state=None
                capture_files=5
                await page.locator('#refresh').click()
                await page.locator('#next-step').filter(has_text='5 HTML files staged').wait_for()
                capture_files=6
                await page.locator('#next-step').filter(has_text='6 HTML files staged').wait_for(timeout=8000)
                assert await page.locator('#capture-activity').is_hidden()
                assert 'Last child paragraph.' in await pane.locator('pre').inner_text()
                await pane.get_by_role('button',name='captured-guild EQ archive',exact=True).click()
                await pane.get_by_label('Capture version').select_option('1')
                await pane.locator('pre').filter(has_text='Later EverQuest guild history.').wait_for()
                await pane.get_by_label('Capture version').select_option('0')
                await pane.get_by_label('Capture version').select_option('1')
                await page.wait_for_timeout(500)
                assert 'Later EverQuest guild history.' in await pane.locator('pre').inner_text()
                # A slow source response from the previous site cannot replace this site.
                await pane.get_by_label('Capture version').select_option('0')
                loading,ready=asyncio.Event(),asyncio.Event()
                async def held_site(route):
                    response=await route.fetch()
                    loading.set()
                    await ready.wait()
                    await route.fulfill(response=response)
                await page.route('**/api/site?**',held_site)
                await other.get_by_role('button',name='Review site',exact=True).click()
                await asyncio.wait_for(loading.wait(),timeout=3)
                assert not await pane.get_by_role('button',name='Approve site & queue indexing').count()
                ready.set()
                await pane.locator('pre').filter(has_text='captured-class site content.').wait_for()
                await page.unroute('**/api/site?**',held_site)
                assert 'captured-guild site content.' not in await pane.locator('pre').inner_text()
                await guild.get_by_role('button',name='Review site',exact=True).click()
                await pane.get_by_role('button',name='Guild child guide',exact=True).click()
                await pane.locator('pre').filter(has_text='Last child paragraph.').wait_for()
                await pane.get_by_role('button',name='Decline indexing',exact=True).click()
                await pane.locator('.site-status').filter(has_text='Indexing declined').wait_for()
                assert 'Last child paragraph.' in await pane.locator('pre').inner_text()
                assert 'Awaiting indexing decision' in await other.inner_text()
                bookmark=page.url
                await page.reload()
                await pane.locator('.site-status').filter(has_text='Indexing declined').wait_for()
                assert page.url==bookmark
                assert await page.locator('#filter').input_value()=='captured'
                await guild.get_by_role('button',name='Review site',exact=True).click()
                await pane.locator('.site-status').filter(has_text='Indexing declined').wait_for()
                await pane.get_by_role('button',name='Reconsider indexing',exact=True).click()
                await pane.get_by_role('button',name='Approve site & queue indexing',exact=True).wait_for()
                await pane.get_by_role('button',name='Guild child guide',exact=True).click()
                await pane.locator('pre').filter(has_text='Last child paragraph.').wait_for()
                await page.screenshot(path=f'/tmp/eqarchives-curation-simple-{width}.png',full_page=True)
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not external,external
                await context.close()
            # The worker can drain the last page while an operator is viewing it.
            context=await browser.new_context(viewport={'width':390,'height':900})
            page=await context.new_page()
            snapshot=await context.request.get(base+'/api/queue?filter=all')
            template=(await snapshot.json())['candidates'][0]
            remaining=61
            async def draining_queue(route):
                from urllib.parse import parse_qs,urlsplit
                requested=int(parse_qs(urlsplit(route.request.url).query)['offset'][0])
                candidates=[{**template,'id':f'{index:032x}','state':'approved_waiting_batch',
                    'decision':{'capture_after':'2030-01-01T00:00:00+00:00'}} for index in range(remaining)]
                await route.fulfill(status=200,content_type='application/json',body=json.dumps({
                    'candidates':candidates[requested:requested+50],'total':remaining,'all_count':remaining,
                    'approved':remaining,'capturing':0,'captured':0,'operations':[],'batches':[],'version':'queue-fixture'}))
            await page.route('**/api/queue?**',draining_queue)
            await page.goto(base)
            await page.locator('#page').filter(has_text='1–50 of 61').wait_for()
            await page.locator('#next').click()
            await page.locator('#page').filter(has_text='51–61 of 61').wait_for()
            remaining=47
            await page.locator('#page').filter(has_text=re.compile(r'^1–47 of 47$')).wait_for(timeout=8000,state='attached')
            assert await page.locator('#candidates article').count()==47
            assert await page.locator('#previous').is_disabled()
            assert await page.locator('.pagination').is_hidden()
            await context.close()
            context=await browser.new_context(viewport={'width':390,'height':900})
            page=await context.new_page()
            await page.goto(base)
            await page.get_by_role('button',name='Review & indexing',exact=True).click()
            pane=page.locator('#reviewed-site')
            other=page.locator('#candidates article').filter(has=page.get_by_role('link',name='http://captured-class.example/eq/',exact=True))
            guild=page.locator('#candidates article').filter(has=page.get_by_role('link',name='http://captured-guild.example/eq/',exact=True))
            await other.get_by_role('button',name='Review site',exact=True).click()
            await pane.get_by_role('button',name='Decline indexing',exact=True).click()
            await pane.locator('.site-status').filter(has_text='Indexing declined').wait_for()
            await guild.get_by_role('button',name='Review site',exact=True).click()
            await pane.locator('p').filter(has_text='Maximum enrichment spend: $2 for this site.').wait_for()
            async with page.expect_request(lambda request:'/api/site-decision' in request.url) as approval:
                await pane.get_by_role('button',name='Approve site & queue indexing',exact=True).click()
            payload=(await approval.value).post_data_json
            assert set(payload)=={'id','manifest_sha256','decision'} and payload['decision']=='approve'
            await pane.locator('.site-status').filter(has_text='Approved — publication queued').wait_for()
            snapshot=await context.request.get(base+'/api/queue?filter=captured')
            sites=(await snapshot.json())['candidates']
            assert {row['state'] for row in sites}=={'approved_waiting_publication','indexing_declined'}
            approved=await context.request.get(base+'/api/site?id='+payload['id'])
            assert len((await approved.json())['manifest']['captures'])==3
            await page.reload()
            await page.get_by_role('button',name='Review & indexing',exact=True).click()
            await guild.get_by_role('button',name='Review site',exact=True).click()
            await pane.locator('.site-status').filter(has_text='Approved — publication queued').wait_for()
            assert not await pane.get_by_role('button',name='Approve site & queue indexing').count()
            # A paused publication belongs to the site, rather than leaving its
            # main panel saying "queued" while a separate history says "paused".
            snapshot=await context.request.get(base+'/api/queue?filter=captured')
            publish_op=next(op for op in (await snapshot.json())['operations'] if op['kind']=='publish')
            paused=True
            async def publication_status(route):
                response=await route.fetch()
                body=await response.json()
                op={**publish_op,'state':'interrupted' if paused else 'queued','error':'Publication SSH runtime account is missing' if paused else None}
                if '/api/site?' in route.request.url: body['operation']=op
                else: body['operations']=[op]
                await route.fulfill(response=response,json=body)
            async def retry_publication(route):
                nonlocal paused
                assert route.request.post_data_json=={'id':publish_op['id']}
                paused=False
                await route.fulfill(status=202,json={'resumed':publish_op['id']})
            await page.route('**/api/queue?**',publication_status)
            await page.route('**/api/site?**',publication_status)
            await page.route('**/api/resume',retry_publication)
            await pane.get_by_role('button',name='Guild child guide',exact=True).click()
            await pane.locator('pre').filter(has_text='Last child paragraph.').wait_for()
            await page.locator('#refresh').click()
            await pane.locator('.site-status').filter(has_text='Publication paused').wait_for()
            await pane.locator('.site-status').filter(has_text='SSH runtime account').wait_for()
            assert await pane.get_by_role('button',name='Retry publication',exact=True).is_enabled()
            assert await page.locator('#capture-activity').is_hidden()
            assert 'Last child paragraph.' in await pane.locator('pre').inner_text()
            await pane.get_by_role('button',name='Retry publication',exact=True).click()
            await pane.locator('.site-status').filter(has_text='Approved — publication queued').wait_for()
            assert not await pane.get_by_role('button',name='Retry publication',exact=True).count()
            assert 'Last child paragraph.' in await pane.locator('pre').inner_text()
            await page.unroute('**/api/queue?**',publication_status)
            await page.unroute('**/api/site?**',publication_status)
            indexing_retrying=False
            failed_job='eqarchives-captures-'+payload['id']
            async def indexing_status(route):
                response=await route.fetch()
                body=await response.json()
                if '/api/site?' in route.request.url:
                    body.update(state='published_waiting_index' if indexing_retrying else 'index_failed',
                                publication={'commit':'1'*40},
                                job={'attempt':2,'state':'retry_queued'} if indexing_retrying else {'name':failed_job,'state':'index_failed'},
                                operation={**publish_op,'state':'completed','error':None})
                else:
                    body['operations']=[]
                    for row in body['candidates']:
                        if row['coverage']['capture']['review_id']==payload['id']:
                            row['review_state']='published_waiting_index' if indexing_retrying else 'index_failed'
                await route.fulfill(response=response,json=body)
            async def retry_indexing(route):
                nonlocal indexing_retrying
                assert route.request.post_data_json=={'id':payload['id'],'manifest_sha256':payload['manifest_sha256'],'job_name':failed_job}
                indexing_retrying=True
                await route.fulfill(status=202,json={'state':'published_waiting_index','attempt':2})
            await page.route('**/api/queue?**',indexing_status)
            await page.route('**/api/site?**',indexing_status)
            await page.route('**/api/index-retry',retry_indexing)
            await page.locator('#refresh').click()
            await pane.locator('.site-status').filter(has_text='Indexing failed').wait_for()
            assert 'Indexing failed' in await guild.inner_text()
            assert await pane.get_by_role('button',name='Retry indexing',exact=True).is_enabled()
            assert 'same $2 site budget' in await pane.locator('.site-status').inner_text()
            assert 'Last child paragraph.' in await pane.locator('pre').inner_text()
            await pane.get_by_role('button',name='Retry indexing',exact=True).click()
            await pane.locator('.site-status').filter(has_text='Published — waiting to index').wait_for()
            assert not await pane.get_by_role('button',name='Retry indexing',exact=True).count()
            assert not await pane.get_by_role('button',name='Approve site & queue indexing').count()
            assert 'Last child paragraph.' in await pane.locator('pre').inner_text()
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await context.close()
        finally:
            await browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    args=parser.parse_args()
    asyncio.run(check(args.url.rstrip('/')))
    print('Curation browser checks passed: independent site/page/document scrolling at 320, 390, 768, 1280 and 1440 px; direct navigation, bookmarked reviews, progress, Undo races, scope, whole-site decisions and publication/indexing retries verified')
