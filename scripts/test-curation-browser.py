"""Real Chromium checks against the built intranet service with isolated sources."""

import argparse
import asyncio

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
                await card.get_by_role('button',name='Approve capture',exact=True).click()
                await page.locator('#status').filter(has_text='1 approved for capture').wait_for()
                await page.reload()
                await page.locator('#status').filter(has_text='1 approved for capture').wait_for()
                card=page.locator('#candidates article').filter(has=page.get_by_role('link',name='http://guild.example/eq/news.html',exact=True))
                await card.get_by_label('Select for batch').check()
                assert await page.locator('#capture').is_enabled()
                await card.get_by_label('Select for batch').uncheck()
                assert await page.locator('#capture').is_disabled()
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                # Reset this fixture review decision for the next viewport.
                await card.get_by_role('button',name='Defer',exact=True).click()
                await page.locator('#status').filter(has_text='0 approved for capture').wait_for()
                assert not external,external
                await context.close()
            context=await browser.new_context(viewport={'width':390,'height':900})
            page=await context.new_page()
            await page.goto(base)
            button=page.get_by_role('button',name='Approve publication & queue indexing')
            await button.wait_for()
            await page.locator('#batches summary').filter(has_text='Review archive file set').click()
            files=page.get_by_label('Include this capture')
            for checkbox in await files.all():
                await checkbox.uncheck()
            assert await button.is_disabled()
            await files.first.check()
            assert await button.is_enabled()
            await button.click()
            await page.locator('#batches h3').filter(has_text='publication requested').wait_for()
            await page.reload()
            await page.locator('#batches h3').filter(has_text='publication requested').wait_for()
            assert not await page.get_by_role('button',name='Approve publication & queue indexing').count()
            await context.close()
        finally:
            await browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    args=parser.parse_args()
    asyncio.run(check(args.url.rstrip('/')))
    print('Curation browser checks passed at 320, 390 and 1280 px; source races, scope, durable decisions and publication approval verified')
