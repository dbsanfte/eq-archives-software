"""Real Chromium checks against the built intranet service with isolated sources."""

import argparse
import asyncio
import json
import re

from playwright.async_api import async_playwright


async def check(base):
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch()
        try:
            for width in (320,390,1280):
                context=await browser.new_context(viewport={'width':width,'height':900})
                page=await context.new_page()
                external=[]
                page.on('request',lambda request: external.append(request.url) if not request.url.startswith(base) else None)
                await page.goto(base)
                await page.locator('#status').filter(has_text='3 candidates').wait_for()
                assert await page.locator('#candidates article').count()==2
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert await page.evaluate('window.pwned') is None
                await page.locator('#filter').select_option('all')
                await page.locator('#status').filter(has_text='3 in this view').wait_for()
                assert await page.locator('#candidates article').count()==3
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
                await page.locator('#filter').select_option('recommended')
                await page.locator('#status').filter(has_text='1 in this view').wait_for()
                assert await page.locator('#candidates article').count()==1
                assert not await page.locator('#candidates').get_by_role('link',name='http://guild.example/eq/news.html',exact=True).count()
                await page.get_by_role('button',name='View awaiting capture',exact=True).click()
                await page.locator('#candidates').get_by_role('button',name='Undo approval',exact=True).wait_for()
                await page.reload()
                await page.locator('#status').filter(has_text='1 awaiting capture').wait_for()
                await page.get_by_role('button',name='View awaiting capture',exact=True).click()
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
                await page.locator('#filter').select_option('recommended')
                release.set()
                await page.locator('#status').filter(has_text='2 in this view').wait_for(timeout=8000)
                assert await page.locator('#candidates article').count()==2
                await page.unroute('**/api/queue?**',held_refresh)
                await page.locator('#filter').select_option('recommended')
                await page.locator('#status').filter(has_text='2 in this view').wait_for()
                await page.locator('#filter').select_option('all')
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
                    if virtual_state:
                        from urllib.parse import parse_qs,urlsplit
                        chosen=parse_qs(urlsplit(route.request.url).query)['filter'][0]
                        current={**virtual_row,'state':virtual_state,'coverage':{**virtual_row['coverage'],'capture':{'batch_id':'a'*32}}}
                        body['candidates']=[current] if chosen in ('all','capturing') and virtual_state=='capturing' or chosen in ('all','captured') and virtual_state=='captured_awaiting_review' else []
                        body['total']=len(body['candidates'])
                        body['capturing']=int(virtual_state=='capturing')
                        body['captured']=int(virtual_state=='captured_awaiting_review')
                    await route.fulfill(status=200,content_type='application/json',body=json.dumps(body))
                await page.route('**/api/queue?**',active_operation)
                await page.locator('#refresh').click()
                await page.locator('#activity h3').filter(has_text='Capturing sites').wait_for()
                await card.locator('summary').click()
                await card.locator('pre').filter(has_text='Early EverQuest guild history.').wait_for()
                source_text=await card.locator('pre').inner_text()
                await page.locator('#batches summary').filter(has_text='Review archive file set').click()
                included=page.get_by_label('Include this capture')
                await included.first.uncheck()
                capture_files=3
                await page.locator('#activity .capture-counts').filter(has_text='3 HTML files staged').wait_for(timeout=8000)
                assert await card.locator('pre').inner_text()==source_text
                assert not await included.first.is_checked()
                assert await page.get_by_role('button',name='Approve publication & queue indexing').is_enabled()
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
                await page.locator('#filter').select_option('capturing')
                await page.locator('#status').filter(has_text='1 capturing').wait_for()
                assert not await page.get_by_role('button',name='Undo approval',exact=True).count()
                virtual_state='captured_awaiting_review'
                await page.locator('#refresh').click()
                await page.locator('#activity h3').filter(has_text='Capture complete').wait_for()
                await page.get_by_role('button',name='View recent captures',exact=True).click()
                await page.locator('#status').filter(has_text='1 in this view').wait_for()
                assert await page.locator('#filter').input_value()=='captured'
                assert await page.locator('#candidates article').count()==1
                await page.locator('#batches .batch-status h3').filter(has_text='Downloaded — review required').wait_for()
                assert not await page.get_by_role('button',name='Undo approval',exact=True).count()
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
            await page.locator('#page').filter(has_text=re.compile(r'^1–47 of 47$')).wait_for(timeout=8000)
            assert await page.locator('#candidates article').count()==47
            assert await page.locator('#previous').is_disabled()
            await context.close()
            context=await browser.new_context(viewport={'width':390,'height':900})
            page=await context.new_page()
            await page.goto(base)
            button=page.get_by_role('button',name='Approve publication & queue indexing')
            await button.wait_for()
            await page.locator('#batches p').filter(has_text='AI enrichment on import').wait_for()
            await page.locator('#batches summary').filter(has_text='Review archive file set').click()
            files=page.get_by_label('Include this capture')
            for checkbox in await files.all():
                await checkbox.uncheck()
            assert await button.is_disabled()
            await files.first.check()
            assert await button.is_enabled()
            await button.click()
            await page.locator('#batches h3').filter(has_text='Publishing approved files').wait_for()
            await page.reload()
            await page.locator('#batches h3').filter(has_text='Publishing approved files').wait_for()
            assert not await page.get_by_role('button',name='Approve publication & queue indexing').count()
            await context.close()
        finally:
            await browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    args=parser.parse_args()
    asyncio.run(check(args.url.rstrip('/')))
    print('Curation browser checks passed at 320, 390 and 1280 px; queue views, progress, Undo/refresh races, draining pagination, scope and publication verified')
