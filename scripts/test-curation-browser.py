"""Real browser regressions for the mobile site workflow; isolated sources only."""
import argparse
import asyncio
import copy
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs,urlsplit

from playwright.async_api import async_playwright, expect


async def settled(page):
    await page.wait_for_function('()=>!busy && data!==null')


async def stage(page,name):
    if name in ('candidates','queued','capturing','indexing'):
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


async def theme_flow(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},
        is_mobile=width<700,has_touch=width<700,color_scheme='dark')
    page=await context.new_page();errors=[];posts=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:posts.append(request.url) if request.method=='POST' else None)
    await page.goto(base);await settled(page)
    toggle=page.get_by_role('button',name='Dark mode',exact=True)
    await expect(toggle).to_have_attribute('aria-pressed','true')
    await expect(page.locator('html')).to_have_attribute('data-theme','dark')
    box=await toggle.bounding_box();spend=await page.locator('#luna-spend summary').bounding_box()
    assert box['width']>=44 and box['height']>=44
    assert box['x']+box['width']<=spend['x'] or box['y']>=spend['y']+spend['height']
    # Text and controls must stay legible on every themed surface.
    async def contrast():
        failures=await page.evaluate('''() => {
          const rgb=s=>s.match(/[\\d.]+/g).map(Number);
          const luminance=c=>c.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
          const failures=[];
          for (const el of document.querySelectorAll('p,h1,h2,label,summary,a,button,input,select,textarea,.meta,.badge,.panel-label,.document-text')) {
            if (!el.checkVisibility() || el.disabled || getComputedStyle(el).opacity<1) continue;
            let parent=el,bg;
            while(parent) {bg=rgb(getComputedStyle(parent).backgroundColor);if(bg.length===3 || bg[3]===1)break;parent=parent.parentElement;}
            const fg=rgb(getComputedStyle(el).color),a=luminance(fg),b=luminance(bg);
            const ratio=(Math.max(a,b)+.05)/(Math.min(a,b)+.05);
            if(ratio<4.5) failures.push([el.className || el.id || el.tagName,ratio]);
          }
          return failures;
        }''')
        assert not failures,failures
    await contrast();await safe_layout(page,width)
    await page.screenshot(path=f'/tmp/curation-theme-dark-{width}.png',full_page=True)
    # Follow OS changes until a choice is made, then persist that override.
    await page.emulate_media(color_scheme='light')
    await expect(toggle).to_have_attribute('aria-pressed','false')
    await page.get_by_label('Luna spend details').click()
    popover=await page.locator('#spend-status').bounding_box()
    controls=await page.locator('.header-actions').bounding_box()
    assert popover['y']>=controls['y']+controls['height'],(popover,controls)
    if width<700: await toggle.tap()
    else: await toggle.focus();await page.keyboard.press('Enter')
    await expect(toggle).to_have_attribute('aria-pressed','true')
    await page.get_by_label('Luna spend details').click()
    await page.reload();await settled(page)
    await expect(toggle).to_have_attribute('aria-pressed','true')
    await page.emulate_media(color_scheme='dark');await page.emulate_media(color_scheme='light')
    await expect(toggle).to_have_attribute('aria-pressed','true')
    await open_site(page,'guild.example/eq/')
    await page.get_by_label('Download scope',exact=True).select_option('custom')
    await page.get_by_label('Custom capture folder path').fill('/theme-draft/')
    await toggle.click();await page.evaluate('refresh()');await settled(page)
    await expect(page.get_by_label('Custom capture folder path')).to_have_value('/theme-draft/')
    await toggle.click()
    await page.get_by_role('button',name='Read source evidence · 2 captures').click()
    await page.get_by_role('button',name='Read guild EQ archive',exact=True).click()
    source=page.locator('.document-text')
    await expect(source).to_contain_text('Early EverQuest guild history.')
    url=page.url
    await contrast()
    await page.screenshot(path=f'/tmp/curation-theme-reader-dark-{width}.png',full_page=True)
    if width>=1000:
        assert await page.locator('.page-browser').evaluate('e=>getComputedStyle(e).backgroundColor') != await page.locator('.document-reader').evaluate('e=>getComputedStyle(e).backgroundColor')
    await toggle.click();await page.evaluate('refresh()');await settled(page)
    assert page.url==url
    await expect(source).to_contain_text('Early EverQuest guild history.')
    await contrast();await safe_layout(page,width)
    await page.reload();await expect(source).to_contain_text('Early EverQuest guild history.')
    await expect(toggle).to_have_attribute('aria-pressed','false')
    await page.screenshot(path=f'/tmp/curation-theme-reader-light-{width}.png',full_page=True)
    # Secondary dialogs inherit the same palette.
    await toggle.click();await page.get_by_role('button',name='Open tools and history').click()
    await contrast();await page.get_by_role('button',name='Close tools',exact=True).click()
    assert not errors and not posts,(errors,posts)
    await context.close()
    if width==320:
        context=await browser.new_context(viewport={'width':width,'height':844},color_scheme='dark')
        await context.add_init_script("Object.defineProperty(window,'localStorage',{get(){throw new Error('Storage blocked')}})")
        page=await context.new_page();await page.goto(base);await settled(page)
        toggle=page.get_by_role('button',name='Dark mode',exact=True)
        await expect(toggle).to_have_attribute('aria-pressed','true')
        await toggle.click();await expect(toggle).to_have_attribute('aria-pressed','false')
        await context.close()


async def basic_flow(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page()
    errors=[];external=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:external.append(request.url) if not request.url.startswith(base) else None)
    await page.goto(base)
    await page.locator('.site-tile').nth(1).wait_for()
    assert await page.locator('.site-tile').count()==2  # The real API hides the grade-1 fixture by default.
    await page.get_by_role('slider',name='Minimum Luna grade').press('Home')
    await page.locator('.site-tile').nth(2).wait_for()
    assert await page.locator('#stages button').count()==4
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
    await page.get_by_role('button',name='Back to site',exact=True).click()
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
    assert await page.locator('#notice').is_hidden()
    assert await page.locator('[data-count=queued]').inner_text()=='1'
    assert await page.locator('[data-count=candidates]').inner_text()=='2'
    await safe_layout(page,width)
    await stage(page,'queued')
    undo=page.get_by_role('button',name='Undo approval: guild.example/research/',exact=True)
    assert await undo.is_visible()
    # Undo during an in-flight refresh remains a real action, never a dropped click.
    loading,release=asyncio.Event(),asyncio.Event()
    async def held_listing(route):
        response=await route.fetch();loading.set();await release.wait()
        try: await route.fulfill(response=response)
        except Exception: pass  # Superseded status requests can be aborted.
    await page.route('**/api/queue?**',held_listing)
    await page.locator('#refresh').click()
    await asyncio.wait_for(loading.wait(),timeout=3)
    await undo.click()
    release.set()
    await page.wait_for_function('()=>!busy && data.stage_counts.queued===0')
    await page.unroute('**/api/queue?**',held_listing)
    await stage(page,'candidates')
    await page.get_by_role('button',name='Open guild.example/research/',exact=True).wait_for()
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
    await stage(page,'indexing')
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
    await page.get_by_role('button',name='Back to site',exact=True).click()
    assert await page.get_by_role('button',name='Approve site & index',exact=True).count()==0
    await page.get_by_role('heading',name='Preparing automatic indexing',exact=True).wait_for()
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
    detail['candidate'].update(stage='indexing',state='approved_waiting_publication',review_state='publication_requested')
    detail['review'].update(state='publication_requested',operation={'id':'d'*32,'kind':'publish','state':'queued','payload':{}})
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
            body['total']=len(body['candidates']);body['operations']=[{'id':'c'*32,'kind':'capture','state':'running','payload':{}}]
            body['stage_counts']={name:int(name==detail['candidate']['stage']) for name in ('candidates','queued','capturing','indexing','saved','history')}
        else:await route.continue_();return
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=indexing&candidate='+row['id']);await settled(page)
    detail['review']['operation']['state']='interrupted';detail['review']['operation']['error']='Publication transport failed; sources retained'
    await page.locator('#refresh').click()
    await page.get_by_role('heading',name='Publication paused',exact=True).wait_for()
    assert await page.get_by_role('button',name='Retry publication',exact=True).is_enabled(),'Capture must not block publication retry'
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
        body={**snapshot,'candidates':rows[offset:offset+50],'total':len(rows),'approved':remaining,'stage_counts':{'candidates':0,'queued':remaining,'capturing':0,'indexing':0,'saved':0,'history':0}}
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
                  'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','indexing','saved','history')}}
        else:raise AssertionError(parsed.path)
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates&candidate='+row['id'])
    await page.get_by_role('heading',name='No archived source found',exact=True).wait_for()
    await page.get_by_text('Technical detail',exact=True).click()
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
                      'operations':[],'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','indexing','saved','history')}}
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
                'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','indexing','saved','history')},
                'operations':[{'id':'b'*32,'kind':'publish','state':'running','payload':{}},
                              *([context_data['capture_operation']] if context_data['capture_operation'] else [])]}
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
    assert await page.get_by_role('button',name='Resume capture',exact=True).is_enabled(),'Publication must not block capture resume'
    await page.get_by_role('button',name='Resume capture',exact=True).click()
    await page.get_by_role('heading',name='Capture in progress',exact=True).wait_for()
    op['result']['progress']['files']=9
    await page.locator('#stage-live').filter(has_text='9 HTML files staged').wait_for(timeout=8000)
    # A completed download changes only this selected site's workspace and stage.
    completed_list=await (await context.request.get(base+'/api/queue?filter=review')).json()
    completed=completed_list['candidates'][0]
    completed_context=await (await context.request.get(base+'/api/candidate?id='+completed['id'])).json()
    row.update(stage='indexing',state='captured_awaiting_review')
    context_data['review']=completed_context['review']
    op['state']='completed'
    await page.locator('#refresh').click()
    await page.get_by_role('heading',name='Your captured site',exact=True).wait_for()
    assert 'view=indexing' in page.url
    assert await page.locator('[data-count=capturing]').inner_text()=='0'
    assert await page.locator('[data-count=indexing]').inner_text()=='1'
    assert not await page.get_by_role('button',name='Approve site & index',exact=True).count()
    await context.close()


async def capture_progress_flow(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page();errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    row=copy.deepcopy(snapshot['candidates'][0]);row.update(stage='capturing',state='capturing')
    progress={'phase':'preparing','files':64,'bytes':1809952,'urls_checked':0,'sites_total':1,
              'site_url':row['scope'],'current_url':row['url']}
    op={'id':'f'*32,'kind':'capture','state':'running','payload':{'sites':[{'id':row['id']}]},'result':{'progress':progress}}
    detail={'candidate':row,'review':None,'capture_operation':op,'queue_blocker':None}
    async def fixture(route):
        assert route.request.method=='GET', 'Progress polling must never start or resume work'
        parsed=urlsplit(route.request.url)
        if parsed.path=='/api/candidate': body=detail
        elif parsed.path=='/api/queue':
            body={**snapshot,'candidates':[row],'total':1,'operations':[op],
                  'stage_counts':{name:int(name=='capturing') for name in ('candidates','queued','capturing','indexing','saved','history')}}
        else: await route.continue_();return
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=capturing');await settled(page)
    await page.get_by_text('Counting remaining captures…',exact=True).wait_for()
    assert await page.get_by_role('progressbar').get_attribute('value') is None
    progress.update(phase='downloading',versions_found=100,versions_pending=36,unavailable=0,catalogs_pending=0,
        completion={'total':100,'completed':64,'remaining':36,'eta_seconds':3600,'updated_at':datetime.now(timezone.utc).isoformat()})
    # An unchanged candidate row still gets updated card progress on polling.
    await page.evaluate('refresh()');await settled(page)
    await page.get_by_text('36 captures left',exact=True).wait_for()
    await expect(page.get_by_role('progressbar')).to_have_attribute('value','64')
    await expect(page.get_by_role('progressbar')).to_have_attribute('max','100')
    await expect(page.locator('.capture-eta')).to_contain_text('ETA: about')
    await page.get_by_role('button',name='Open '+await page.locator('.site-tile h2').inner_text(),exact=True).click()
    await settled(page)
    meter=page.locator('#stage-live .capture-meter')
    await expect(meter).to_contain_text('36 captures left')
    await expect(meter).to_contain_text('Counts cover the files listed so far')
    progress.update(files=5000,versions_found=20000,versions_pending=15000,catalogs_pending=2,failed_lookups=1,excluded_urls=3,
        completion={'total':20000,'completed':5000,'remaining':15000,'eta_seconds':5400,'updated_at':datetime.now(timezone.utc).isoformat()})
    await page.evaluate('refresh()');await settled(page)
    await expect(meter).to_contain_text('15,000 captures left')
    await expect(meter).to_contain_text('5,000 of 20,000 listed captures processed · 25%')
    await expect(meter.get_by_role('progressbar')).to_have_attribute('max','20000')
    await expect(meter).to_contain_text('2 archive listings still being checked.')
    await expect(meter).to_contain_text('1 supporting-file lookups failed. Other files continue downloading')
    await expect(meter).to_contain_text('3 advertising URLs intentionally skipped (ad.* and ads.*).')
    await page.reload();await settled(page)
    await expect(meter).to_contain_text('15,000 captures left')
    await expect(meter.locator('.capture-eta')).to_contain_text('ETA: about')
    # A stalled replay cannot make the countdown claim that work finished.
    await meter.locator('.capture-eta').evaluate('(node)=>node.dataset.finish=String(Date.now()-1)')
    await expect(meter.locator('.capture-eta')).to_have_text('ETA updating: waiting for the next capture.',timeout=2500)
    op['state']='interrupted';op['error']='Wayback request budget reached'
    await page.evaluate('refresh()');await settled(page)
    await expect(meter.locator('.capture-eta')).to_have_text('ETA paused. Recalculates after resume.')
    await expect(meter.get_by_role('progressbar')).to_have_attribute('value','5000')
    op['state']='running';op.pop('error');progress['completion']['eta_seconds']=None
    await page.evaluate('refresh()');await settled(page)
    await expect(meter.locator('.capture-eta')).to_have_text('ETA calculating after the next captures…')
    progress['completion'].update(eta_seconds=5400,updated_at=datetime.now(timezone.utc).isoformat())
    await page.evaluate('refresh()');await settled(page)
    await safe_layout(page,width)
    await page.screenshot(path=f'/tmp/curation-capture-progress-{width}.png',full_page=True)
    assert not errors,errors
    await context.close()


async def regenerate_capture_flow(browser,base,width,retry_files=False):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page();posts=[];errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    row=copy.deepcopy(snapshot['candidates'][0]);row.update(stage='indexing',state='captured_awaiting_review')
    row['coverage']['capture']={'needs_regeneration':True,'pages':1,'files':2}
    manifest={'sites':[row],'captures':row['captures'],'limits':{'files':100},
              'capture_coverage':{row['id']:{'state':'bounded','reason':'Legacy batch file limit reached'}}}
    reviewed={'id':'e'*32,'manifest_sha256':'f'*64,'state':'awaiting_review','manifest':manifest}
    if retry_files:
        manifest.update(capture_policy='complete-files-v1',capture_retry={'files':1,'lookups':1},
            notes=[{'url':'http://counter.example/i.gif','note':'Supporting-file lookup failed: Wayback HTTP 403'}])
        row['coverage']['capture']['needs_regeneration']=False
        row.update(state='published',review_state='published_waiting_index')
        reviewed['state']='published_waiting_index'
    action='Retry failed files' if retry_files else 'Regenerate full capture'
    undo='Undo file retry' if retry_files else 'Undo regeneration'
    reject=True
    async def fixture(route):
        path=urlsplit(route.request.url).path
        if route.request.method=='POST':
            posts.append((path,route.request.post_data_json))
            if path=='/api/continue-capture':
                assert route.request.post_data_json=={'id':reviewed['id'],'manifest_sha256':reviewed['manifest_sha256']}
                if reject:
                    await route.fulfill(status=409,json={'error':'Captured site changed; refresh before regeneration.'});return
                row.update(stage='queued',state='approved_waiting_batch')
                row['coverage']['capture']['continuation']={'review_id':reviewed['id'],**({'mode':'retry_failed'} if retry_files else {})}
                reviewed['state']='capture_continued'
            elif path=='/api/undo':
                row.update(stage='indexing',state='captured_awaiting_review')
                row['coverage']['capture'].pop('continuation')
                reviewed['state']='published_waiting_index' if retry_files else 'awaiting_review'
                row['review_state']=reviewed['state']
            else:raise AssertionError(path)
            await route.fulfill(status=202,json={'state':row['state']});return
        if path=='/api/candidate':body={'candidate':row,'review':reviewed,'capture_operation':None}
        elif path=='/api/queue':
            view=parse_qs(urlsplit(route.request.url).query)['filter'][0]
            body={**snapshot,'candidates':[row] if view==row['stage'] else [],'total':int(view==row['stage']),'operations':[],
                  'review_actions':{'count':1,'incomplete_count':0 if retry_files else 1},
                  'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','indexing','saved','history')}}
        else:await route.continue_();return
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=review');await settled(page)
    await page.locator('.site-tile').click();await settled(page)
    if retry_files:
        await expect(page.get_by_role('button',name='Approve site & index',exact=True)).to_have_count(0)
        await page.get_by_text('Read coverage notes',exact=True).click()
        await expect(page.locator('#site-workspace')).to_contain_text('Supporting-file lookup failed: Wayback HTTP 403')
    else:
        await page.get_by_role('heading',name='Full capture needed',exact=True).wait_for()
        assert await page.get_by_role('button',name='Approve site & index',exact=True).count()==0
    await page.get_by_role('button',name=action,exact=True).click();await settled(page)
    await expect(page.locator('#error')).to_contain_text('Captured site changed')
    assert 'view=indexing' in page.url
    reject=False
    await page.get_by_role('button',name=action,exact=True).click();await settled(page)
    assert 'view=queued' in page.url
    await page.reload();await settled(page)
    await page.get_by_role('button',name=undo,exact=True).click();await settled(page)
    assert 'view=indexing' in page.url
    await expect(page.get_by_role('button',name=action,exact=True)).to_be_enabled()
    await safe_layout(page,width)
    await page.screenshot(path=f'/tmp/curation-{"retry-files" if retry_files else "regeneration"}-{width}.png',full_page=True)
    assert len(posts)==3 and not errors,(posts,errors)
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
    reject=False;arrivals=[];workers=None
    original=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    template=original['candidates'][0]
    async def fixture(route):
        nonlocal operations
        path=urlsplit(route.request.url).path
        if route.request.method=='POST':
            posts.append((path,route.request.post_data_json))
            assert path in ('/api/discover','/api/resume'),path
            if reject:
                operations=[{'id':'c'*32,'kind':'capture','state':'running','payload':{}}]
                await route.fulfill(status=409,json={'error':'Capture has already started.'});return
            submitted.set();await release_action.wait()
            if path=='/api/discover':
                assert route.request.post_data_json=={'max_candidates':50,'max_usd':2,'min_grade':2}
                operations=[{'id':'b'*32,'kind':'discover','state':'queued','payload':route.request.post_data_json},
                            *[op for op in operations if op['kind']=='publish']]
            else:
                assert route.request.post_data_json=={'id':'b'*32}
                operations[0]['state']='queued'
            await route.fulfill(status=202,json={'operation':'b'*32});return
        assert path=='/api/queue',path
        loading.set();await release_listing.wait()
        await route.fulfill(status=200,json={'candidates':arrivals,'total':len(arrivals),'offset':0,'operations':operations,'workers':workers,
            'stage_counts':{name:0 for name in ('candidates','queued','capturing','indexing','saved','history')},
            'capture_queue_error':None,'version':'discovery-fixture'})
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates')
    await asyncio.wait_for(loading.wait(),timeout=3)
    button=page.locator('#discover');status=page.locator('#discovery-status')
    assert await page.locator('#stage-list #discover').is_visible(),'Discovery must be on Candidates, outside More'
    assert await button.is_disabled(),'Availability must be checked before enabling a paid action'
    release_listing.set();await settled(page)
    await status.filter(has_text='Ready. Publication and indexing run separately.').wait_for()
    assert await button.is_enabled() and not posts
    await page.locator('#manual-url').fill('http://typed-while-busy.example/')
    assert await page.locator('#manual-submit').is_enabled()
    assert 'discovery-status' in await button.get_attribute('aria-describedby')
    assert '50 candidates' in await page.locator('#discovery-limits').inner_text()
    assert '$2' in await page.locator('#discovery-limits').inner_text()
    await safe_layout(page,width)
    bounds=await button.bounding_box()
    assert bounds['height']>=48 and bounds['width']>=44
    assert bounds['y']>=0 and bounds['y']+bounds['height']<min(760,await page.evaluate('innerHeight'))
    await page.screenshot(path=f'/tmp/curation-discovery-during-publication-{width}.png',full_page=True)
    await page.get_by_role('button',name='Open tools and history').click()
    assert not await page.locator('#tools #discover').count()
    await page.get_by_role('button',name='Close tools',exact=True).click()
    for name in ('capturing','queued','indexing'):
        await stage(page,name)
        assert await button.is_hidden(),name
    await stage(page,'candidates')
    assert await button.is_visible()
    # Only another acquisition blocks the serial Wayback worker.
    for kind,state,text in (('capture','running','Capture is running.'),('discover','running','Discovery is running.')):
        operations=[{'id':'a'*32,'kind':kind,'state':state,'payload':{'max_candidates':50,'max_usd':2}}]
        await page.locator('#refresh').click()
        await status.filter(has_text=text).wait_for()
        assert await button.is_disabled()
    # Worker availability must survive a long publication backlog that fills
    # the capped recent-operations list.
    workers={'capture':operations[0],'indexing':None}
    operations=[{'id':f'{i:032x}','kind':'publish','state':'queued','payload':{}} for i in range(20)]
    await page.locator('#refresh').click();await settled(page)
    assert await button.is_disabled()
    assert 'Discovery is running.' in await status.inner_text()
    workers=None
    operations=[{'id':'a'*32,'kind':'publish','state':'queued','payload':{}}]
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
    # Results and progress arrive through the normal poll while the worker is busy.
    operations[0].update(state='running',payload={'max_candidates':50,'max_usd':2,'min_grade':2,'fill_queue':True},
        result={'progress':{'phase':'grading','accepted':1,'checked':4,'target':50,'min_grade':2,'deadline':9999999999,
                            'estimated_usd':.01,'reserved_usd':.02,'max_usd':2}})
    arrivals.append(copy.deepcopy(template))
    await expect(page.locator('#discovery-progress-text')).to_contain_text('1/50 new Grade 2+ sites',timeout=8000)
    await expect(page.locator('#candidates .site-tile')).to_have_count(1)
    assert await page.locator('#discovery-meter').get_attribute('value')=='1'
    assert len(posts)==1 and await button.is_disabled()
    await page.reload();await settled(page)
    await expect(page.locator('#discovery-progress-text')).to_contain_text('4 checked')
    await safe_layout(page,width)
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
    await page.locator('#error').filter(has_text='Capture has already started.').wait_for()
    # A worker claim between polling and clicking refreshes the visible blocker.
    await status.filter(has_text='Capture is running.').wait_for()
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
                    for name in ('candidates','queued','capturing','indexing','saved','history')}})
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
    # There is no approval popup; Undo is directly on the queued entry.
    assert await page.locator('#notice').is_hidden()
    await stage(page,'queued')
    await page.get_by_role('button',name='Undo approval: fixture-50.example',exact=True).click()
    await page.wait_for_function('()=>!busy && data.stage_counts.queued===0')
    assert posts[-1][0]=='/api/undo' and posts[-1][1]['id']==rows[50]['id']
    assert 'candidate=' not in page.url and await page.locator('#site-workspace').is_hidden()
    await page.goto(base+'/?view=candidates&offset=50&search=fixture-')
    await page.locator('#page').filter(has_text='51–67 of 67').wait_for()
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


async def close_notification(page,width,selector='#notice'):
    box=page.locator(selector);close=box.get_by_role('button',name='Close notification',exact=True)
    await close.scroll_into_view_if_needed()
    bounds=await close.bounding_box();content=await box.locator('.notification-content').bounding_box()
    assert bounds['width']>=44 and bounds['height']>=44
    assert content['x']+content['width']<=bounds['x'] and bounds['x']+bounds['width']<=width
    assert await close.evaluate('(el)=>{const r=el.getBoundingClientRect();return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}')
    location=page.url;posts=[]
    def posted(request):
        if request.method=='POST':posts.append(request.url)
    page.on('request',posted)
    if width<1000:await close.tap()
    else:await close.focus();await page.keyboard.press('Enter')
    await expect(box).to_be_hidden()
    await page.evaluate('refresh()');await settled(page)
    assert await box.is_hidden() and page.url==location and not posts, {'hidden':await box.is_hidden(),'url':page.url,'expected':location,'posts':posts}
    page.remove_listener('request',posted)


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
                    for name in ('candidates','queued','capturing','indexing','saved','history')}})
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
    # Closing the popup dismisses only its message. The rejection survives
    # polling, and remains reversible through the site's History workspace.
    await close_notification(page,width)
    assert rows[0]['stage']=='history' and not await tile(0).count()
    await stage(page,'history');await open_site(page,'swipe-0.example/eq/')
    await page.get_by_role('button',name='Restore to Candidates',exact=True).click();await settled(page)
    await page.get_by_role('button',name='Dark mode',exact=True).click()
    assert await page.locator('#notice button[data-mutation]').count()==0
    await close_notification(page,width)  # success message without Undo, dark palette
    await stage(page,'candidates');await tile(0).wait_for();await settled(page)
    # Exercise the equivalent button here; the earlier swipe already tested
    # touch dismissal. Navigation can legitimately cancel an in-flight gesture.
    await page.get_by_role('button',name='Dismiss: swipe-0.example/eq/',exact=True).click();await settled(page)
    await undo.click();await tile(0).wait_for();await settled(page)
    assert posts[-1][0]=='/api/restore' and rows[0]['stage']=='candidates'
    await decide(0,True)
    await page.locator('#error').filter(has_text='Coverage changed').wait_for()
    assert await tile(0).count() and rows[0]['stage']=='candidates'
    assert 'candidate=' not in page.url
    await close_notification(page,width,'#error')
    await decide(0,True);await asyncio.wait_for(submitted.wait(),timeout=3)
    count=len(posts)
    assert await page.get_by_role('button',name='Approve capture: swipe-0.example/eq/',exact=True).is_disabled()
    if width<1000:await swipe(2,125)
    assert len(posts)==count and rows[0]['stage']=='candidates'
    release.set();await page.wait_for_function('()=>!busy && data.stage_counts.queued===1')
    assert not await tile(0).count() and rows[0]['stage']=='queued'
    assert await page.locator('#notice').is_hidden()
    await safe_layout(page,width)
    await stage(page,'queued')
    await page.get_by_role('button',name='Undo approval: swipe-0.example/eq/',exact=True).click()
    await page.wait_for_function('()=>!busy && data.stage_counts.queued===0')
    await stage(page,'candidates');await tile(0).wait_for();await settled(page)
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
    # Closing a recurring polling error stays closed until an explicit retry
    # or a new failure. It must not reappear on every five-second update.
    async def failed_refresh(route):
        await route.fulfill(status=503,json={'error':'Fixture refresh is temporarily unavailable.'})
    await page.route('**/api/queue?**',failed_refresh)
    await page.locator('#refresh').click()
    await expect(page.locator('#error')).to_contain_text('Fixture refresh is temporarily unavailable.')
    await close_notification(page,width,'#error')
    await page.locator('#refresh').click()
    await expect(page.locator('#error')).to_be_visible()
    await close_notification(page,width,'#error')
    await page.unroute('**/api/queue?**',failed_refresh)
    assert not errors,errors
    await context.close()


async def candidate_grade_controls(browser,base,width):
    """Filter races, evidence recovery and bulk decisions with all APIs intercepted."""
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page();errors=[];posts=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    template=snapshot['candidates'][0]
    rows=[]
    for index,grade in enumerate([0,1,2,3]+[3]*51+[None,None,None]):
        row=copy.deepcopy(template)
        address=f'http://grade-{grade if grade is not None else "ungraded"}-{index}.example/'
        row.update(id=f'{index:024x}',url=address,scope=address,
                   stage='candidates',state='approval_pending' if grade is not None else 'unavailable',review_state=None,candidate_check=None)
        if grade is not None:row['rating']['grade']=grade
        else:row.update(rating=None,captures=[],error='No exact HTML captures found within the two tiers')
        rows.append(row)
    missing,saved,retry=rows[-3:]
    saved.update(captures=copy.deepcopy(template['captures']),state='approval_pending',error=None)
    retry.update(state='grade_error',captures=copy.deepcopy(template['captures']),error='Luna response incomplete; no judgment saved',
                 candidate_check={'id':'a'*32,'state':'interrupted','error':'Luna response incomplete; no judgment saved','phase':'grading','max_usd':.25})
    queued=copy.deepcopy(template);queued.update(id='f'*24,stage='queued',state='approved_waiting_batch',review_state=None)
    rows.append(queued)
    operations=[];dismissed=[];stale=True;revision=0
    async def fixture(route):
        nonlocal stale,revision
        request=route.request;parsed=urlsplit(request.url);query=parse_qs(parsed.query)
        if request.method=='POST':
            payload=request.post_data_json;posts.append((parsed.path,payload))
            if parsed.path=='/api/check-candidate':
                row=next(row for row in rows if row['id']==payload['id'])
                assert payload=={'id':row['id'],'manifest_sha256':row['manifest_sha256'],'max_usd':2}
                prior=row['candidate_check'];identifier=prior['id'] if prior else 'b'*32
                row['candidate_check']={'id':identifier,'state':'queued','phase':None,'error':None,'max_usd':prior['max_usd'] if prior else 2}
                operations[:]=[{'id':identifier,'kind':'candidate_check','state':'queued','payload':payload,'result':None,'error':None}]
                await route.fulfill(status=202,json={'operation':identifier,'max_usd':row['candidate_check']['max_usd']});return
            if parsed.path=='/api/dismiss-candidates':
                assert payload=={'token':str(revision)}
                if stale:
                    stale=False;revision+=1
                    await route.fulfill(status=409,json={'error':'Candidates changed. Review the new count and try Dismiss all again.'});return
                dismissed[:]=[row for row in rows if row['stage']=='candidates']
                for row in dismissed:row.update(stage='history',state='rejected')
                revision+=1
                await route.fulfill(status=200,json={'dismissed':len(dismissed),'dismissal':'d'*32});return
            if parsed.path=='/api/undo-dismissal':
                assert payload=={'dismissal':'d'*32}
                for row in dismissed:row.update(stage='candidates',state='approval_pending')
                revision+=1
                await route.fulfill(status=200,json={'restored':len(dismissed)});return
            if parsed.path=='/api/scope':
                assert payload=={'id':saved['id'],'manifest_sha256':saved['manifest_sha256'],'mode':'custom','path':'/research/'}
                saved.update(scope=saved['url']+'research/',scope_mode='custom',manifest_sha256='c'*64)
                await route.fulfill(status=200,json={'saved':True});return
            raise AssertionError(parsed.path)  # No paid or production mutations can escape.
        if parsed.path=='/api/candidate':
            await route.fulfill(status=200,json={'candidate':next(row for row in rows if row['id']==query['id'][0]),'review':None});return
        assert parsed.path=='/api/queue'
        view=query['filter'][0];minimum=int(query.get('min_grade',['0'])[0]);ungraded=query.get('needs_grade')==['1']
        candidates=[row for row in rows if row['stage']=='candidates']
        selected=[row for row in rows if row['stage']==view]
        if view=='candidates':
            selected=[row for row in selected if not row['rating']] if ungraded else [row for row in selected if row['rating'] and row['rating']['grade']>=minimum]
            selected.sort(key=lambda row:(-(row['rating'] or {}).get('grade',-1),row['url']))
        selected=[row for row in selected if query.get('search',[''])[0] in row['url']]
        offset=int(query.get('offset',['0'])[0])
        body={**snapshot,'candidates':selected[offset:offset+50],'total':len(selected),'operations':copy.deepcopy(operations),
              'stage_counts':{name:sum(row['stage']==name for row in rows) for name in snapshot['stage_counts']},
              'candidate_grades':{str(grade):sum((row['rating'] or {}).get('grade',-1)==grade for row in candidates) for grade in range(-1,4)},
              'dismissal':{'count':len(candidates),'token':str(revision)}}
        if minimum==1:await asyncio.sleep(.65)
        try:await route.fulfill(status=200,json=body)
        except Exception:pass  # Obsolete responses are intentionally aborted.
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates');await settled(page)
    slider=page.get_by_role('slider',name='Minimum Luna grade')
    assert await slider.input_value()=='2'
    await expect(page.locator('.site-tile')).to_have_count(50)
    assert (await page.locator('.site-tile h2').first.inner_text()).startswith('grade-3-')
    await page.locator('#next').click();await settled(page)
    await expect(page.locator('.site-tile')).to_have_count(3)
    await slider.press('Home');await page.locator('#minimum-grade-value').filter(has_text='0').wait_for()
    await page.get_by_label('Find a site',exact=True).fill('grade-0-')
    await page.locator('.site-tile h2').filter(has_text='grade-0-0.example').wait_for()
    assert 'offset=' not in page.url
    await page.get_by_label('Find a site',exact=True).fill('');await page.wait_for_timeout(250)
    await slider.press('ArrowRight');await page.wait_for_timeout(250)
    await slider.press('End')
    await page.locator('#view-total').filter(has_text='52 sites').wait_for();await page.wait_for_timeout(750)
    assert all(name.startswith('grade-3-') for name in await page.locator('.site-tile h2').all_text_contents())
    await page.reload();await settled(page)
    assert await slider.input_value()=='3' and 'min_grade=3' in page.url
    await page.goto(base);await settled(page)
    assert await slider.input_value()=='3'  # Preference survives a fresh portal visit.
    await page.locator('#ungraded-view').click();await settled(page)
    await expect(page.locator('.candidate-card')).to_have_count(3)
    await page.go_back();await settled(page)
    assert await page.locator('#graded-view').get_attribute('aria-pressed')=='true'
    await page.locator('#ungraded-view').click();await settled(page)
    find=page.get_by_role('button',name='Find samples & grade: '+urlsplit(missing['scope']).netloc,exact=True)
    await expect(find).to_be_enabled()
    await expect(page.get_by_role('button',name='Approve capture: '+urlsplit(missing['scope']).netloc,exact=True)).to_be_disabled()
    await expect(page.get_by_role('button',name='Grade source evidence: '+urlsplit(saved['scope']).netloc,exact=True)).to_be_enabled()
    retry_button=page.get_by_role('button',name='Retry evidence & grading: '+urlsplit(retry['scope']).netloc,exact=True)
    await retry_button.click();await settled(page)
    assert len(posts)==1 and operations[0]['id']=='a'*32
    await expect(find).to_be_disabled()
    retry['candidate_check'].update(state='running',phase='grading');operations[0]['state']='running'
    await page.locator('#refresh').click();await settled(page)
    await page.get_by_role('button',name='Grading with Luna: '+urlsplit(retry['scope']).netloc,exact=True).wait_for()
    retry['candidate_check'].update(state='interrupted',error='Luna response incomplete; no judgment saved')
    operations[0].update(state='interrupted',error='Luna response incomplete; no judgment saved')
    await page.locator('#refresh').click();await settled(page)
    await expect(retry_button).to_be_enabled()
    assert 'Original $0.25 cap' in await retry_button.locator('..').inner_text()
    await open_site(page,urlsplit(saved['scope']).netloc)
    await page.get_by_label('Download scope',exact=True).select_option('custom')
    await page.get_by_label('Custom capture folder path',exact=True).fill('/research/')
    await page.get_by_role('button',name='Grade source evidence: '+urlsplit(saved['scope']).netloc,exact=True).click();await settled(page)
    saved.update(rating=copy.deepcopy(template['rating']),state='approval_pending',manifest_sha256='e'*64);saved['rating']['grade']=3
    saved['candidate_check']['state']='completed';operations.clear()
    await page.locator('#refresh').click();await settled(page)
    await expect(page.get_by_text('Luna grade 3/3 · guild',exact=True)).to_be_visible()
    await expect(page.get_by_label('Custom capture folder path',exact=True)).to_have_value('/research/')
    await expect(page.get_by_role('button',name='Approve site for capture',exact=True)).to_be_disabled()
    await page.get_by_role('button',name='Save capture scope',exact=True).click();await settled(page)
    await expect(page.get_by_role('button',name='Approve site for capture',exact=True)).to_be_enabled()
    await stage(page,'candidates');await page.locator('#graded-view').click();await settled(page)
    await page.get_by_label('Find a site',exact=True).fill('grade-3-3.')
    await page.locator('#view-total').filter(has_text='1 site').wait_for()
    await page.locator('#dismiss-all').click()
    assert 'all 58 remaining candidates' in await page.locator('#dismiss-description').inner_text()
    assert 'hidden by grade or search filters and other pages' in await page.locator('#dismiss-description').inner_text()
    await safe_layout(page,width)
    await page.locator('#dismiss-cancel').click()
    assert len(posts)==3
    await page.locator('#dismiss-all').click();await page.locator('#dismiss-confirm').click();await settled(page)
    await page.locator('#error').filter(has_text='Candidates changed').wait_for()
    assert await page.locator('[data-count=candidates]').inner_text()=='58'
    await page.locator('#refresh').click();await settled(page)
    await page.locator('#dismiss-all').click();await page.locator('#dismiss-confirm').click();await settled(page)
    assert await page.locator('[data-count=candidates]').inner_text()=='0'
    assert await page.locator('[data-count=queued]').inner_text()=='1'
    await page.get_by_role('button',name='Undo dismiss all',exact=True).click();await settled(page)
    assert await page.locator('[data-count=candidates]').inner_text()=='58'
    await page.get_by_label('Find a site',exact=True).fill('');await settled(page)
    await page.locator('#ungraded-view').click();await settled(page)
    await expect(page.locator('.candidate-card')).to_have_count(2)
    await page.screenshot(path=f'/tmp/curation-candidate-grades-{width}.png',full_page=True)
    await safe_layout(page,width)
    assert not errors,errors
    await context.close()


async def automatic_indexing_flow(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page();posts=[];errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    snapshot=await (await context.request.get(base+'/api/queue?filter=review')).json()
    row=snapshot['candidates'][0]
    detail=await (await context.request.get(base+'/api/candidate?id='+row['id'])).json()
    row=detail['candidate'];review=detail['review']
    async def fixture(route):
        parsed=urlsplit(route.request.url)
        if route.request.method=='POST':
            payload=route.request.post_data_json;posts.append((parsed.path,payload))
            assert parsed.path=='/api/prepare-indexing'
            assert payload=={'id':review['id'],'manifest_sha256':review['manifest_sha256']}
            review.update(state='awaiting_review',error=None);row['review_state']='awaiting_review'
            await route.fulfill(status=202,json={'state':'awaiting_review'});return
        if parsed.path=='/api/candidate':body=detail
        elif parsed.path=='/api/queue':
            view=parse_qs(parsed.query)['filter'][0]
            body={**snapshot,'candidates':[row] if view==row['stage'] else [],'total':int(view==row['stage']),'operations':[],
                  'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','indexing','saved','history')}}
        else:await route.continue_();return
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    # Saved Review links resolve to the new stage, including reader navigation.
    await page.goto(base+'/?view=review&candidate='+row['id']);await settled(page)
    assert 'view=indexing' in page.url
    await expect(page.locator('#stages button')).to_have_count(4)
    await expect(page.locator('[data-view=review],#review-actions,#review-dialog')).to_have_count(0)
    await expect(page.get_by_role('heading',name='Preparing automatic indexing',exact=True)).to_be_visible()
    await expect(page.locator('#stage-live')).to_contain_text('up to $2 for this site, including retries')
    assert not posts
    review.update(state='index_preflight_failed',error='Archive coverage is unverified. Retry after checking coverage.')
    row['review_state']='index_preflight_failed'
    await page.evaluate('refresh()');await settled(page)
    await expect(page.get_by_role('heading',name='Indexing preparation paused',exact=True)).to_be_visible()
    await expect(page.locator('#stage-live')).to_contain_text(review['error'])
    await page.reload();await settled(page)
    assert not posts
    await page.get_by_role('button',name='Retry indexing preparation',exact=True).click();await settled(page)
    await expect(page.get_by_role('heading',name='Preparing automatic indexing',exact=True)).to_be_visible()
    assert len(posts)==1
    review.update(state='publication_requested');row['review_state']='publication_requested'
    await page.evaluate('refresh()');await settled(page)
    await expect(page.get_by_role('heading',name='Publishing approved files',exact=True)).to_be_visible()
    await expect(page.get_by_role('button',name='Approve site & index',exact=True)).to_have_count(0)
    await safe_layout(page,width)
    await page.screenshot(path=f'/tmp/curation-automatic-indexing-{width}.png',full_page=True)
    review.update(state='indexed');row.update(state='indexed',review_state='indexed',stage='history')
    await page.evaluate('refresh()');await settled(page)
    await expect(page.get_by_role('heading',name='Indexed · complete',exact=True)).to_be_visible()
    assert 'view=history' in page.url
    await stage(page,'indexing');await expect(page.locator('.site-tile')).to_have_count(0)
    assert len(posts)==1 and not errors,(posts,errors)
    await context.close()


async def ezboard_flow(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page();errors=[];posts=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    row=copy.deepcopy(snapshot['candidates'][0])
    url='http://pub4.ezboard.com/beqasylum'
    row.update(url=url,scope=url,scope_mode='ezboard',ezboard='eqasylum',stage='candidates',state='approval_pending')
    row['coverage']={'site_check':{'status':'new_site','complete':True}}
    review=None
    async def fixture(route):
        nonlocal review
        parsed=urlsplit(route.request.url)
        if route.request.method=='POST':
            assert parsed.path=='/api/scope'
            payload=route.request.post_data_json;posts.append(payload)
            assert payload['mode']=='ezboard' and payload['id']==row['id']
            row['manifest_sha256']='d'*64
            await route.fulfill(status=200,json={'scope':url});return
        if parsed.path=='/api/queue':
            body={**snapshot,'operations':[],'candidates':[row],'total':1,
                  'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','indexing','saved','history')}}
        elif parsed.path=='/api/candidate':body={'candidate':row,'review':review}
        elif parsed.path=='/api/source':body={**review['manifest']['captures'][0],'complete_extracted_text':'EverQuest Lanys thread, replies 21 through 37.'}
        else:raise AssertionError(parsed.path)
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates&candidate='+row['id']);await settled(page)
    assert await page.get_by_label('Download scope',exact=True).input_value()=='ezboard'
    await page.get_by_text('Capture window: 1 January 1999–31 December 2006.',exact=False).wait_for()
    await page.get_by_text('Board identity: eqasylum.',exact=False).wait_for()
    await safe_layout(page,width)
    await page.get_by_label('Download scope',exact=True).select_option('page')
    await page.get_by_label('Download scope',exact=True).select_option('ezboard')
    await page.get_by_role('button',name='Save capture scope',exact=True).click();await settled(page)
    assert len(posts)==1 and await page.get_by_label('Download scope',exact=True).input_value()=='ezboard'
    source=copy.deepcopy(row['captures'][0])
    source.update(url='http://pub110.ezboard.com/feqasylumfrm25.showMessageRange?topicID=2358.topic&start=21&stop=37',
                  timestamp='20020602023020',title='Lanys raid thread',candidate_id=row['id'])
    row.update(stage='indexing',state='captured_awaiting_review',review_state='awaiting_review')
    review={'id':'c'*32,'state':'awaiting_review','manifest_sha256':'e'*64,'page_identities':[source['url']],'source_slots':[0],
            'manifest':{'sites':[row],'captures':[source],'notes':[{'url':source['url'],'reason':'Unavailable archived discussion'}],
                        'capture_window':{'from':'19990101000000','to':'20061231235959','versions':'all_available'},
                        'capture_coverage':{row['id']:{'state':'bounded','reason':'Full 1999–2006 date coverage is incomplete; file limit reached'}},
                        'ezboard':{'coverage':{'state':'bounded','hosts':2,'forums':22,'catalogs_remaining':3,
                          'reason':'Capture file limit reached; pending captures are retained','counts':{'excluded':1,'unavailable':2}}}}}
    await page.goto(base+'/?view=review&candidate='+row['id']);await settled(page)
    await page.get_by_text('Capture file limit reached; pending captures are retained',exact=True).wait_for()
    await page.get_by_text('3 captures unavailable or excluded',exact=False).wait_for()
    await page.get_by_text('Requested window:',exact=False).filter(has_text='2006').wait_for()
    await page.get_by_text('Full 1999–2006 date coverage is incomplete; file limit reached',exact=True).wait_for()
    await page.get_by_text('Read coverage notes',exact=True).click()
    await page.get_by_text(source['url']+': Unavailable archived discussion',exact=True).wait_for()
    await safe_layout(page,width)
    await page.screenshot(path=f'/tmp/curation-ezboard-{width}.png',full_page=True)
    await page.get_by_role('button',name='Browse 1 captured page',exact=True).click()
    await page.get_by_role('button',name='Read Lanys raid thread',exact=True).click()
    await page.locator('.document-text').filter(has_text='replies 21 through 37').wait_for()
    links=await page.locator('a[href*="web.archive.org/web/20020602023020/"]').all()
    hrefs=[await link.get_attribute('href') for link in links]
    assert hrefs and any(source['url'] in href for href in hrefs)
    assert not errors,errors
    await context.close()


async def candidate_failure_feedback(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page();errors=[];posts=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    row=copy.deepcopy(snapshot['candidates'][0])
    raw='No exact HTML captures found within the two tiers'
    row.update(url='http://www.freeservers.com/cgi-bin/redirect?id=ezboard-r1',
               scope='http://www.freeservers.com/cgi-bin/redirect/',stage='candidates',state='unavailable',
               captures=[],rating=None,error=raw,review_state=None,
               candidate_check={'id':'e'*32,'state':'interrupted','phase':'sampling','error':raw,'max_usd':2})
    async def fixture(route):
        parsed=urlsplit(route.request.url)
        if route.request.method!='GET':posts.append(parsed.path);raise AssertionError('Feedback must never start work')
        if parsed.path=='/api/candidate':body={'candidate':row,'review':None}
        elif parsed.path=='/api/queue':
            body={**snapshot,'candidates':[row],'total':1,'operations':[],
                  'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','indexing','saved','history')}}
        else:raise AssertionError(parsed.path)
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates&needs_grade=1');await settled(page)
    await page.locator('.candidate-issue-summary').filter(has_text='no matching HTML page').wait_for()
    await open_site(page,'www.freeservers.com/cgi-bin/redirect/')
    alert=page.locator('.candidate-issue[role=alert]')
    await alert.get_by_role('heading',name='No archived source found',exact=True).wait_for()
    await alert.get_by_text('Retry checks the same URL again.',exact=False).wait_for()
    assert await alert.get_by_role('link',name='Check this URL in Wayback').get_attribute('href')=='https://web.archive.org/web/*/'+row['url']
    assert (await alert.bounding_box())['y'] < (await page.locator('#site-workspace .candidate-recovery').first.bounding_box())['y']
    await alert.get_by_text('Technical detail',exact=True).click()
    await alert.get_by_text(raw,exact=True).wait_for()
    await safe_layout(page,width)
    await page.screenshot(path=f'/tmp/curation-candidate-error-{width}.png',full_page=True)
    # A new error can arrive with unchanged candidate state and manifest hash.
    row['error']='Wayback HTTP 429';row['candidate_check']['error']=row['error']
    await page.evaluate('refresh()')
    await alert.get_by_role('heading',name='Wayback is limiting requests',exact=True).wait_for()
    await alert.get_by_text('Technical detail',exact=True).click()
    await alert.get_by_text('Wayback HTTP 429',exact=True).wait_for()
    row['candidate_check'].update(state='queued',error=None)
    await page.evaluate('refresh()')
    assert not await page.locator('.candidate-issue').count()  # Never show a stale failure as the current result.
    row['error']='Complete source exceeds grading limit; leave candidate unjudged'
    row['candidate_check'].update(state='interrupted',phase='grading',error=row['error'])
    row['state']='grade_error'
    await page.evaluate('refresh()')
    await alert.get_by_role('heading',name='Source is too large to grade',exact=True).wait_for()
    await alert.get_by_text('Retrying cannot reduce its size.',exact=False).wait_for()
    row.update(state='rejected',stage='history')
    row['coverage']['candidate_exclusion']='FreeServers hosting signup advertisement from Ezboard footers. Submit the EQ site’s direct URL instead.'
    await page.goto(base+'/?view=history&candidate='+row['id']);await settled(page)
    await alert.get_by_role('heading',name='Excluded promotion',exact=True).wait_for()
    await alert.get_by_text('hosting signup advertisement',exact=False).wait_for()
    assert not await page.get_by_role('button',name='Restore to Candidates',exact=True).count()
    await page.get_by_role('button',name='Back to Candidates',exact=True).wait_for()
    await safe_layout(page,width)
    assert not errors and not posts,(errors,posts)
    await context.close()


async def sitepowerup_flow(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page();errors=[];posts=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    row=copy.deepcopy(snapshot['candidates'][0])
    url='http://www.sitepowerup.com/mb/view.asp?Action=Display&BoardID=102010'
    row.update(url=url,scope=url,scope_mode='sitepowerup',sitepowerup='102010',ezboard=None,
               stage='candidates',state='approval_pending')
    row['coverage']={'site_check':{'status':'new_site','complete':True}}
    review=None
    async def fixture(route):
        parsed=urlsplit(route.request.url)
        if route.request.method=='POST':
            submitted=route.request.post_data_json
            payload=submitted[0] if parsed.path=='/api/decisions' else submitted
            posts.append((parsed.path,payload))
            assert payload['id']==row['id']
            if parsed.path=='/api/scope':
                assert payload['mode']=='sitepowerup'
                # Saving the original scope does not change the server hash.
                body={'scope':url}
            elif parsed.path=='/api/decisions':
                assert payload['decision']=='approve'
                row.update(stage='queued',state='approved_waiting_batch',decision={'capture_after':'2030-01-01T00:00:00Z'})
                body={'status':'approved'}
            else:raise AssertionError(parsed.path)
            await route.fulfill(status=200,json=body);return
        if parsed.path=='/api/queue':
            body={**snapshot,'operations':[],'candidates':[row],'total':1,
                  'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','indexing','saved','history')}}
        elif parsed.path=='/api/candidate':body={'candidate':row,'review':review}
        elif parsed.path=='/api/source':body={**review['manifest']['captures'][0],
                                           'complete_extracted_text':'Archived Enchanter message. Exact reply identity retained.'}
        else:raise AssertionError(parsed.path)
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates&candidate='+row['id']);await settled(page)
    await page.get_by_role('heading',name='SitePowerUp · Board 102010',exact=True).wait_for()
    scope=page.get_by_label('Download scope',exact=True)
    assert await scope.input_value()=='sitepowerup'
    assert await scope.locator('option').count()==2
    await page.get_by_text('BoardID: 102010.',exact=False).wait_for()
    await scope.select_option('page');await scope.select_option('sitepowerup')
    await page.get_by_role('button',name='Save capture scope',exact=True).click();await settled(page)
    await page.get_by_role('button',name='Approve site for capture',exact=True).click();await settled(page)
    assert [path for path,_ in posts]==['/api/scope','/api/decisions']
    assert parse_qs(urlsplit(page.url).query)['view']==['candidates']
    assert await page.locator('#notice').is_hidden()
    await stage(page,'queued')
    await page.get_by_role('button',name='Undo approval: SitePowerUp · Board 102010',exact=True).wait_for()
    await safe_layout(page,width)
    source=copy.deepcopy(row['captures'][0])
    source.update(url='http://www.sitepowerup.com/mb/view.asp?Action=Reply&BoardID=102010&Reply=12155',
                  timestamp='20000109050003',title='Enchanter message',candidate_id=row['id'])
    row.update(stage='indexing',state='captured_awaiting_review',review_state='awaiting_review')
    review={'id':'c'*32,'state':'awaiting_review','manifest_sha256':'e'*64,
            'page_identities':[source['url']],'source_slots':[0],
            'manifest':{'sites':[row],'captures':[source],'notes':[],
                        'capture_window':{'from':'19990101000000','to':'20061231235959','versions':'all_available'},
                        'sitepowerup':{'board':'102010','coverage':{'state':'bounded','catalogs_remaining':3,
                          'reason':'Capture file limit reached; pending captures are retained','counts':{'captured':1}}}}}
    await page.goto(base+'/?view=review&candidate='+row['id']);await settled(page)
    await page.get_by_text('SitePowerUp BoardID 102010 · 3 catalog queries remaining',exact=False).wait_for()
    await page.get_by_text('Requested window:',exact=False).filter(has_text='2006').wait_for()
    await page.screenshot(path=f'/tmp/curation-sitepowerup-{width}.png',full_page=True)
    await page.get_by_role('button',name='Browse 1 captured page',exact=True).click()
    await page.get_by_role('button',name='Read Enchanter message',exact=True).click()
    await page.locator('.document-text').filter(has_text='Exact reply identity retained.').wait_for()
    links=await page.locator('a[href*="web.archive.org/web/20000109050003/"]').all()
    hrefs=[await link.get_attribute('href') for link in links]
    assert hrefs and any(source['url'] in href for href in hrefs)
    await safe_layout(page,width)
    assert not errors,errors
    await context.close()


async def advanced_grading_flow(browser,base,width):
    """Custom focus is explicit, persistent and visible; every paid POST is mocked."""
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page();posts=[];errors=[];operations=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    row=copy.deepcopy(snapshot['candidates'][0]);row['candidate_check']=None
    async def fixture(route):
        path=urlsplit(route.request.url).path
        if route.request.method=='POST':
            payload=route.request.post_data_json;posts.append((path,payload))
            if path=='/api/resume':
                assert payload=={'id':operations[0]['id']}
                operations[0]['state']='queued'
            else:
                assert path in ('/api/discover','/api/submit-site','/api/check-candidate'),path
                operation={'id':'b'*32,'state':'queued','kind':'candidate_check' if path=='/api/check-candidate' else 'discover',
                           'payload':{'max_candidates':1 if path!='/api/discover' else 50,**payload}}
                if path=='/api/submit-site':operation['payload']['target']={'url':payload['url']}
                operations.append(operation)
                if path=='/api/check-candidate':
                    row['candidate_check']={'id':operation['id'],'state':'queued','max_usd':.25,'grading_criteria':payload['grading_criteria']}
            await route.fulfill(status=202,json={'operation':'b'*32,**({'url':payload['url']} if path=='/api/submit-site' else {})});return
        if path=='/api/queue':
            await route.fulfill(status=200,json={**snapshot,'candidates':[row],'total':1,'operations':operations})
        elif path=='/api/candidate':
            await route.fulfill(status=200,json={'candidate':row,'review':None})
        else:raise AssertionError(path)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=candidates');await settled(page)
    await page.locator('#grading-advanced summary').click()
    focus=page.get_by_label('Additional grading criteria',exact=True)
    criteria='Cleric class sites\nHealing guides and contemporary advice'
    await focus.fill(criteria)
    await page.evaluate('refresh()')
    assert await focus.input_value()==criteria and not posts
    await safe_layout(page,width)
    await page.reload();await settled(page)
    await page.locator('#grading-advanced summary').click()
    assert await focus.input_value()==criteria and not posts
    await page.locator('#manual-url').fill('http://clerics.example/')
    await page.locator('#manual-submit').click();await settled(page)
    assert posts[-1]==('/api/submit-site',{'url':'http://clerics.example/','max_usd':2,'grading_criteria':criteria})
    operations.clear();await page.evaluate('refresh()')
    await page.locator('#discover').click();await settled(page)
    assert posts[-1]==('/api/discover',{'max_candidates':50,'max_usd':2,'min_grade':2,'grading_criteria':criteria})
    operations[0]['state']='interrupted'
    await page.evaluate('refresh()')
    await focus.fill('Guild communities')
    await expect(page.locator('#run-criteria')).to_contain_text(criteria)
    await page.get_by_role('button',name='Resume discovery',exact=True).first.click();await settled(page)
    assert posts[-1]==('/api/resume',{'id':'b'*32}) and operations[0]['payload']['grading_criteria']==criteria
    operations.clear();await page.evaluate('refresh()')
    await page.get_by_role('button',name='Use default grading',exact=True).click()
    assert await focus.input_value()==''
    await page.locator('#discover').click();await settled(page)
    assert posts[-1]==('/api/discover',{'max_candidates':50,'max_usd':2,'min_grade':2})
    operations.clear();await page.evaluate('refresh()')
    await page.locator('.site-tile').first.click()
    await page.locator('#site-workspace h1').wait_for();await settled(page)
    await page.get_by_text('Advanced · Grade this site with Luna',exact=True).click()
    local=page.get_by_label('Additional grading criteria for this site',exact=True)
    regrade=page.get_by_role('button',name='Regrade with these criteria',exact=True)
    assert await local.input_value()=='' and await regrade.is_disabled()
    await local.press_sequentially('Cleric sites',delay=10)
    # Publication leaves grading available; a capture still blocks it. Both
    # status changes must preserve the criteria draft during workspace updates.
    operations.append({'id':'c'*32,'kind':'publish','state':'running','payload':{}})
    await page.evaluate('refresh()')
    assert await local.input_value()=='Cleric sites' and await regrade.is_enabled()
    operations[0]['kind']='capture';await page.evaluate('refresh()')
    assert await local.input_value()=='Cleric sites' and await regrade.is_disabled()
    operations[0]['kind']='publish';await page.evaluate('refresh()')
    assert await regrade.is_enabled()
    await regrade.click();await settled(page)
    assert posts[-1][0]=='/api/check-candidate' and posts[-1][1]['grading_criteria']=='Cleric sites'
    assert await page.get_by_role('button',name='Approve site for capture',exact=True).is_disabled()
    assert await local.is_disabled()
    row['rating']={**row['rating'],'grade':2,'grading_criteria':'Cleric sites'}
    row['manifest_sha256']='f'*64;row['candidate_check']['state']='completed';operations.clear()
    await page.evaluate('refresh()')
    await expect(page.locator('#site-workspace .grading-focus')).to_have_text('Graded for: Cleric sites')
    assert await regrade.is_disabled() and await local.is_enabled()
    assert await page.get_by_role('button',name='Approve site for capture',exact=True).is_enabled()
    await expect(page.locator('.grading-options.panel')).to_contain_text('original $0.25 total cap')
    await safe_layout(page,width)
    await page.screenshot(path=f'/tmp/curation-grading-criteria-{width}.png',full_page=True)
    assert not errors,errors
    await context.close()


async def complete_file_review(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=width<700)
    page=await context.new_page();errors=[];requests=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    snapshot=await (await context.request.get(base+'/api/queue?filter=candidates')).json()
    row=copy.deepcopy(snapshot['candidates'][0]);row.update(stage='indexing',state='captured_awaiting_review',review_state='awaiting_review')
    page_capture={**row['captures'][0],'kind':'page','candidate_id':row['id']}
    files=[{**page_capture,'url':row['scope']+f'images/sword-{i}.png','kind':'file','title':'',
            'content_type':'image/png','timestamp':'20061231235959','bytes':1234} for i in range(1100)]
    captures=[page_capture,*files]
    review={'id':'d'*32,'state':'awaiting_review','manifest_sha256':'e'*64,
            'source_slots':list(range(len(captures))),'page_identities':[c['url'] for c in captures],
            'manifest':{'sites':[row],'captures':captures,'capture_policy':'complete-files-v1',
                        'capture_window':{'from':'19990101000000','to':'20061231235959'},
                        'capture_coverage':{row['id']:{'state':'complete','reason':'All listed files checked.'}}}}
    async def fixture(route):
        parsed=urlsplit(route.request.url);query=parse_qs(parsed.query);requests.append((parsed.path,query))
        assert route.request.method=='GET','Polling and reading must not start work'
        if parsed.path=='/api/queue':
            assert query.get('compact')==['1']
            body={**snapshot,'candidates':[row],'total':1,'operations':[],'batches':[],
                  'stage_counts':{name:int(name==row['stage']) for name in ('candidates','queued','capturing','indexing','saved','history')}}
        elif parsed.path=='/api/candidate':
            cached={k:v for k,v in review.items() if k not in ('manifest','source_slots','page_identities')}
            body={'candidate':row,'review':{**cached,'manifest_unchanged':True} if query.get('known_manifest')==['e'*64] else review}
        elif parsed.path=='/api/source':
            slot=int(query.get('slot',['0'])[0]);body={**captures[slot],
                'complete_extracted_text':'EverQuest source page.' if slot==0 else None,
                'file_message':'Original file preserved. Download it or open its dated Wayback capture.'}
        else:raise AssertionError(parsed.path)
        await route.fulfill(status=200,json=body)
    await page.route('**/api/**',fixture)
    await page.goto(base+'/?view=review&candidate='+row['id']);await settled(page)
    await page.get_by_text('1100 supporting files ·',exact=False).wait_for()
    await page.get_by_role('button',name='Browse 1100 supporting files',exact=True).click();await settled(page)
    await expect(page.locator('.site-page')).to_have_count(100)
    await expect(page.get_by_label('Captured content type')).to_have_value('files')
    await page.get_by_role('button',name='Next files',exact=True).click()
    await expect(page.locator('.site-page').first).to_contain_text('sword-100.png')
    await page.locator('.site-page button').first.click();await settled(page)
    await expect(page.locator('.document-text')).to_contain_text('Original file preserved')
    assert 'slot=101' in await page.get_by_role('link',name='Download original file',exact=True).get_attribute('href')
    assert '20061231235959/' in await page.get_by_role('link',name='Open this capture in Wayback',exact=True).get_attribute('href')
    await page.evaluate('refresh()');await settled(page)
    await expect(page.locator('.document-text')).to_contain_text('Original file preserved')
    await page.reload();await settled(page)
    await expect(page.get_by_label('Captured content type')).to_have_value('files')
    await expect(page.locator('.site-page').first).to_contain_text('sword-100.png')
    await expect(page.locator('.document-text')).to_contain_text('Original file preserved')
    await safe_layout(page,width)
    await page.screenshot(path=f'/tmp/curation-complete-files-{width}.png',full_page=True)
    assert any('known_manifest' in query for path,query in requests if path=='/api/candidate')
    assert not errors,errors
    await context.close()


async def check(base):
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch()
        try:
            for width in (320,390,430,768,1280):
                await theme_flow(browser,base,width)
                await regenerate_capture_flow(browser,base,width)
                await regenerate_capture_flow(browser,base,width,retry_files=True)
                await capture_progress_flow(browser,base,width)
                await complete_file_review(browser,base,width)
                await candidate_failure_feedback(browser,base,width)
                await sitepowerup_flow(browser,base,width)
                await advanced_grading_flow(browser,base,width)
                await ezboard_flow(browser,base,width)
                await automatic_indexing_flow(browser,base,width)
                await candidate_grade_controls(browser,base,width)
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
