#!/usr/bin/env python3
"""Browser regressions against the built frontend, with isolated API fixtures."""

import os
import fnmatch
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
import unittest

from playwright.sync_api import expect, sync_playwright


class FrontendBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_url = os.environ["FRONTEND_BASE_URL"]
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browser = playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
        )
        cls.addClassCleanup(cls.browser.close)

    @staticmethod
    def mock_elasticsearch(route):
        if route.request.url.endswith("/_count"):
            route.fulfill(json={"count": 0})
        else:
            facets = {
                field: {"buckets": []}
                for field in (
                    "domain_name", "llm_content_flavour", "file_type",
                    "mime_type", "mailing_list_name", "llm_tags",
                )
            }
            facets["last_indexed"] = {"buckets": {}}
            route.fulfill(json={
                "took": 1,
                "timed_out": False,
                "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
                "aggregations": {"facet_bucket_all": {"doc_count": 0, **facets}},
            })

    def check_sort_menu(self, width, filled_dates):
        context = self.browser.new_context(
            viewport={"width": width, "height": 900},
            is_mobile=width <= 800,
            has_touch=width <= 800,
        )
        try:
            page = context.new_page()
            page.set_default_timeout(10000)
            page.route("**/elasticsearch/**", self.mock_elasticsearch)
            # Let the initial search and facet requests settle before editing.
            page.goto(self.base_url, wait_until="networkidle")
            page.locator(".sui-sorting").wait_for(state="attached")
            expect(page.get_by_role("progressbar")).to_have_count(0)
            show_filters = page.get_by_role("button", name="Show Filters", exact=True)
            if show_filters.is_visible():
                show_filters.click()

            date_facet = page.locator(".archive-date-facet").first

            def open_date_picker(index):
                if width <= 800:
                    date_facet.locator("input").nth(index).click()
                else:
                    date_facet.get_by_role("button", name="Choose date", exact=False).nth(index).click()
                dialog = page.get_by_role("dialog")
                expect(dialog).to_be_visible()
                dialog.evaluate("""dialog => Promise.all(
                    dialog.getAnimations({subtree: true}).map(animation => animation.finished)
                )""")

            if filled_dates:
                for index, day in enumerate((15, 16)):
                    open_date_picker(index)
                    page.get_by_role("gridcell", name=str(day), exact=True).click()
                    confirm = page.get_by_role("button", name="OK", exact=True)
                    if confirm.is_visible():
                        confirm.click()
                    expect(page.get_by_role("dialog", include_hidden=True)).to_have_count(0)

            sorting = page.locator(".sui-sorting")
            sorting.locator(".sui-select__control").click()
            menu = sorting.locator(".sui-select__menu")
            expect(menu).to_be_visible()

            # Hit-test the actual overlapping surfaces instead of asserting a
            # particular z-index. Unfilled MUI labels normally ignore pointers;
            # enabling hit testing briefly does not alter their paint order.
            labels = date_facet.locator(".MuiInputLabel-root").evaluate_all("""labels => {
                const menu = document.querySelector('.sui-sorting .sui-select__menu');
                const menuRect = menu.getBoundingClientRect();
                return labels.map(label => {
                    const rect = label.getBoundingClientRect();
                    const x = rect.left + rect.width / 2;
                    const y = rect.top + rect.height / 2;
                    const pointerEvents = label.style.pointerEvents;
                    try {
                        label.style.pointerEvents = 'auto';
                        const top = document.elementFromPoint(x, y);
                        return {
                            text: label.textContent,
                            floating: label.getAttribute('data-shrink') === 'true',
                            overlapsMenu: x > menuRect.left && x < menuRect.right &&
                                y > menuRect.top && y < menuRect.bottom,
                            menuOnTop: menu.contains(top),
                            topElement: top ? top.outerHTML.slice(0, 250) : null
                        };
                    } finally {
                        label.style.pointerEvents = pointerEvents;
                    }
                });
            }""")
            self.assertEqual([label["text"] for label in labels], ["From Date", "To Date"])
            for label in labels:
                self.assertEqual(label["floating"], filled_dates, label)
                self.assertTrue(label["overlapsMenu"], label)
                self.assertTrue(label["menuOnTop"], label)

            option = "Captured date (Descending)"
            page.get_by_role("option", name=option, exact=True).click()
            expect(menu).to_be_hidden()
            expect(sorting.locator(".sui-select__single-value")).to_have_text(option)

            # The higher menu layer must still allow the date picker to open.
            open_date_picker(0)
        finally:
            context.close()

    def test_sort_menu_covers_empty_and_floating_date_labels(self):
        for width in (320, 390, 768, 1280):
            for filled_dates in (False, True):
                with self.subTest(width=width, filled_dates=filled_dates):
                    self.check_sort_menu(width, filled_dates)

    def test_explore_links_charts_dates_phrases_and_site_search(self):
        for width in (320, 390, 768, 1280):
            with self.subTest(width=width):
                context = self.browser.new_context(viewport={'width': width, 'height': 900},
                    is_mobile=width <= 800, has_touch=width <= 800)
                try:
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    events, errors, searches, pending = [], [], [], []
                    fail_phrases = False
                    hold = False
                    defaults = {'start': '1999-01-01', 'end': '2006-12-31', 'basis': 'capture_date', 'theme': '', 'site': '', 'phrase': ''}
                    domain = 'www.a-very-long-everquest-guild-domain.example.org:8080'

                    def response(kind, selection):
                        base = {'selection': selection, 'generated_at': '2026-10-10T12:00:00Z'}
                        if kind == 'phrases':
                            return {**base, 'sampled_pages': 90, 'sampled_sites': 16, 'examined_captures': 100,
                                'duplicate_captures': 10, 'clipped_pages': 4, 'excerpt_chars': 12000, 'sample_limit': 100,
                                'phrases': [{'text': 'ancient cyclops', 'pages': 20}, {'text': 'cleric', 'pages': 12},
                                            {'text': 'necromancer', 'pages': 20}]}
                        years = range(int(selection['start'][:4]), int(selection['end'][:4]) + 1)
                        return {**base, 'records': 12000, 'tagged': 3000, 'sites_count': 20,
                            'timeline': [{'year': year, 'records': 500 * (i + 1), 'sites': i + 2} for i, year in enumerate(years)],
                            'themes': [{'key': 'raid', 'count': 500, 'approximate': False}, {'key': 'lore', 'count': 200, 'approximate': False}],
                            'sites': [{'key': domain, 'count': 1234, 'approximate': False}]}

                    def explore(route):
                        nonlocal hold
                        params = parse_qs(urlparse(route.request.url).query)
                        selection = {**defaults, **{key: values[0] for key, values in params.items()}}
                        kind = urlparse(route.request.url).path.rsplit('/', 1)[1]
                        events.append((kind, selection))
                        if hold:
                            pending.append((route, response(kind, selection)))
                            return
                        if fail_phrases and kind == 'phrases':
                            return route.fulfill(status=503, json={'error': 'temporary'})
                        route.fulfill(json=response(kind, selection))

                    def archive(route):
                        if route.request.url.endswith('/_search'):
                            searches.append(route.request.post_data_json)
                        return self.mock_elasticsearch(route)

                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.route('**/api/explore/**', explore)
                    page.route('**/elasticsearch/**', archive)
                    page.route('**/openai/v1/embeddings', lambda route: route.fulfill(json={'data': [{'embedding': [0.1] * 768}]}))
                    page.goto(self.base_url + '/explore', wait_until='networkidle')
                    expect(page.get_by_role('heading', name='Explore early EverQuest.')).to_be_visible()
                    expect(page.get_by_role('link', name='Explore', exact=True)).to_have_attribute('aria-current', 'page')
                    def chart(name):
                        if width <= 900:
                            page.get_by_role('navigation', name='Explore charts').get_by_role('button', name=name, exact=True).click()
                            titles = {'Timeline': 'Through the years', 'Themes': 'Follow a theme', 'Words': 'Words from the pages', 'Sites': 'Explore the sites'}
                            heading = page.get_by_role('heading', name=titles[name], exact=True)
                            expect(heading).to_be_visible()
                            # React's scroll effect can run between two protocol
                            # reads. Measure both surfaces in one animation frame,
                            # and wait until scrolling has revealed the heading.
                            page.wait_for_function('''title => {
                                const heading = [...document.querySelectorAll('.explore-panel h2')].find(e => e.textContent === title);
                                const nav = document.querySelector('.explore-panel-nav').getBoundingClientRect();
                                const rect = heading.getBoundingClientRect();
                                const painted = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                                return rect.top >= nav.bottom && rect.bottom <= innerHeight && heading.contains(painted);
                            }''', arg=titles[name])

                    def dates():
                        if not page.locator('.explore-date-settings').get_attribute('open') == '':
                            page.locator('.explore-date-settings > summary').click()

                    if width <= 900:
                        expect(page.locator('.explore-date-settings')).not_to_have_attribute('open', '')
                        expect(page.get_by_role('heading', name='Through the years')).to_be_visible()
                        expect(page.get_by_role('heading', name='Words from the pages')).to_be_hidden()
                    chart('Words')
                    expect(page.get_by_role('button', name='ancient cyclops: 20 sampled pages')).to_be_visible()
                    sizes = page.locator('.explore-cloud button').evaluate_all('els => els.map(e => parseFloat(getComputedStyle(e).fontSize))')
                    self.assertGreaterEqual(max(sizes) / min(sizes), 2.4, 'Cloud frequencies should have a visible size hierarchy')
                    self.assertEqual(sizes[0], sizes[2], 'Equal page counts must have equal sizes')
                    dates()
                    self.assertEqual(searches, [])
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    for selector in ('.archive-primary-navigation a', '.explore-controls input', '.explore-controls select', '.explore-actions a', '.explore-year', '.explore-cloud button'):
                        for height in page.locator(selector).evaluate_all('els => els.filter(e => e.getClientRects().length).map(e => e.getBoundingClientRect().height)'):
                            self.assertGreaterEqual(height, 44)
                    page.get_by_role('button', name='List', exact=True).click()
                    expect(page.get_by_text('20 pages', exact=True).first).to_be_visible()
                    page.get_by_role('button', name='Cloud', exact=True).click()
                    chart('Timeline')
                    page.get_by_role('button', name='Site count', exact=True).click()
                    year = page.get_by_role('button', name='2000: 3 estimated domains. Explore this year')
                    year.tap() if width <= 800 else year.click()
                    expect(page.get_by_label('From', exact=True)).to_have_value('2000-01-01')
                    expect(page.get_by_label('Through', exact=True)).to_have_value('2000-12-31')
                    expect(page.locator('.explore-year')).to_have_count(1)
                    chart('Themes')
                    page.get_by_role('list', name='Themes').get_by_role('button', name='raid 500', exact=True).click()
                    expect(page.get_by_role('button', name='Remove theme: raid')).to_be_visible()
                    chart('Words')
                    word = page.get_by_role('button', name='ancient cyclops: 20 sampled pages')
                    expect(word).to_be_enabled()
                    word.click()
                    expect(page.get_by_role('button', name='Remove phrase: ancient cyclops')).to_be_visible()
                    chart('Sites')
                    site = page.get_by_role('list', name='Contributing sites').get_by_role('button')
                    expect(site).to_be_enabled()
                    site.click()
                    expect(page.get_by_role('button', name=f'Remove site: {domain}')).to_be_visible()
                    page.wait_for_load_state('networkidle')
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    saved = page.url
                    page.reload(wait_until='networkidle')
                    dates()
                    self.assertEqual(page.url, saved)
                    expect(page.get_by_role('button', name=f'Remove site: {domain}')).to_be_visible()
                    # Draft date edits survive an unrelated summary refresh or failure.
                    page.get_by_label('From', exact=True).fill('2000-02-29')
                    chart('Words')
                    fail_phrases = True
                    page.get_by_role('button', name='Check for updates').click()
                    expect(page.get_by_role('alert')).to_contain_text('previous snapshot')
                    expect(page.get_by_label('From', exact=True)).to_have_value('2000-02-29')
                    fail_phrases = False
                    page.get_by_role('button', name='Retry phrases').click()
                    expect(page.get_by_role('alert')).to_have_count(0)
                    page.get_by_label('Date meaning').select_option('llm_guessed_date')
                    page.get_by_role('button', name='Apply dates').click()
                    page.wait_for_load_state('networkidle')
                    search_url = page.get_by_role('link', name='Search these pages', exact=False).get_attribute('href')
                    params = parse_qs(urlparse(search_url).query)
                    self.assertEqual(params['filters[0][field]'], ['llm_guessed_date'])
                    self.assertEqual(params['filters[0][values][0][from]'], ['2000-02-29T00:00:00.000Z'])
                    self.assertEqual(params['filters[0][values][0][to]'], ['2000-12-31T23:59:59.999Z'])
                    self.assertEqual(params['filters[3][values][0]'], [domain])
                    page.get_by_role('link', name='Search these pages', exact=False).click()
                    page.wait_for_load_state('networkidle')
                    expect(page.get_by_role('region', name='Site search scope')).to_contain_text(domain)
                    self.assertTrue(searches)
                    request = json.dumps(searches[-1])
                    for value in (domain, 'llm_guessed_date', '2000-02-29T00:00:00.000Z', '2000-12-31T23:59:59.999Z', 'websites/', 'raid', 'text_full:'):
                        self.assertIn(value, request)
                    page.go_back(wait_until='networkidle')
                    dates()
                    expect(page.get_by_role('heading', name='Explore early EverQuest.')).to_be_visible()
                    expect(page.get_by_role('button', name=f'Remove site: {domain}')).to_be_visible()
                    # Superseded requests cannot paint stale counts under new filters.
                    hold = True
                    page.get_by_role('button', name='1999–2001', exact=True).click()
                    page.wait_for_function("location.search.includes('end=2001-12-31')")
                    page.wait_for_timeout(100)
                    hold = False
                    page.get_by_role('button', name='2002–2006', exact=True).click()
                    expect(page.get_by_label('From', exact=True)).to_have_value('2002-01-01')
                    expect(page.locator('.explore-year')).to_have_count(5)
                    for route, payload in pending:
                        if 'records' in payload:
                            payload['records'] = 777777
                        route.fulfill(json=payload)
                    page.wait_for_timeout(150)
                    expect(page.get_by_text('777,777', exact=True)).to_have_count(0)
                    page.get_by_role('button', name='Reset exploration').click()
                    expect(page.get_by_label('From', exact=True)).to_have_value('1999-01-01')
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_recent_sites_open_a_persistent_focused_search(self):
        for width in (320, 390, 768, 1280):
            with self.subTest(width=width):
                context = self.browser.new_context(
                    viewport={'width': width, 'height': 900},
                    is_mobile=width <= 800, has_touch=width <= 800,
                )
                try:
                    page = context.new_page()
                    searches, lists, embeddings, errors = [], [], [], []
                    fail_list = False
                    domain = 'www.guild.example.org:8080'
                    records = [{'_id': f'websites/{domain}/20000101000000/page{i}.html', '_score': 1,
                        '_source': {'title': f'Guild page {i}', 'domain_name': domain,
                                    'url': f'https://web.archive.org/web/20000101000000/http://{domain}/page{i}.html',
                                    'text_full': '# Original guild page\n\nCleric and paladin spells.',
                                    'capture_date': '2000-01-01T00:00:00Z'}} for i in range(26)]
                    facets = {field: {'buckets': []} for field in (
                        'domain_name', 'llm_content_flavour', 'file_type', 'mime_type', 'mailing_list_name', 'llm_tags'
                    )}
                    facets['domain_name'] = {'buckets': [{'key': domain, 'doc_count': len(records)}]}
                    facets['last_indexed'] = {'buckets': {}}

                    def archive(route):
                        if route.request.url.endswith('/_count'):
                            return route.fulfill(json={'count': len(records)})
                        body = route.request.post_data_json
                        if 'sites' in body.get('aggs', {}):
                            lists.append(body)
                            self.assertEqual(body['size'], 0)
                            self.assertEqual(body['aggs']['sites']['terms']['size'], 50)
                            if fail_list:
                                return route.fulfill(status=503, json={'error': 'test outage'})
                            return route.fulfill(json={'_shards': {'total': 1, 'failed': 0}, 'aggregations': {'sites': {'buckets': [
                                {'key': name, 'doc_count': 123, 'latest_indexed': {'value': 1791624219290 - i * 1000},
                                 'first_capture': {'value': 946684800000}, 'last_capture': {'value': 1167609599000}}
                                for i, name in enumerate([domain, 'a-very-long-historical-everquest-guild-site.example.org', 'older.example.org'])
                            ]}}})
                        if 'ids' in body.get('query', {}):
                            return route.fulfill(json={'hits': {'hits': [record for record in records
                                if record['_id'] in body['query']['ids']['values']]}})
                        searches.append(body)
                        start = body.get('from', 0)
                        hits = [{**record, '_source': {key: value for key, value in record['_source'].items() if key != 'text_full'}}
                                for record in records[start:start + body['size']]]
                        route.fulfill(json={'hits': {'total': {'value': len(records), 'relation': 'eq'}, 'hits': hits},
                            'aggregations': {'facet_bucket_all': {'doc_count': len(records), **facets}}})

                    def embed(route):
                        embeddings.append(route.request.post_data_json)
                        route.fulfill(json={'data': [{'embedding': [0.1] * 768}]})

                    def domain_values(value):
                        found = []
                        if isinstance(value, dict):
                            for key in ('term', 'terms'):
                                item = value.get(key, {}).get('domain_name')
                                if item is not None:
                                    found.extend(item if isinstance(item, list) else [item])
                            for child in value.values():
                                found.extend(domain_values(child))
                        elif isinstance(value, list):
                            for child in value:
                                found.extend(domain_values(child))
                        return found

                    def check_scope():
                        body = searches[-1]
                        self.assertIn(domain, domain_values([body.get('query'), body.get('post_filter')]))
                        for branch in body.get('knn', []):
                            self.assertIn(domain, domain_values(branch.get('filter')))

                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.route('**/elasticsearch/**', archive)
                    page.route('**/openai/v1/embeddings', embed)
                    page.goto(self.base_url, wait_until='networkidle')
                    page.get_by_role('link', name='Recently indexed', exact=True).click()
                    expect(page.get_by_role('heading', name='Recently indexed sites', exact=True)).to_be_visible()
                    expect(page.locator('.site-card')).to_have_count(3)
                    expect(page.get_by_role('link', name='Recently indexed', exact=True)).to_have_attribute('aria-current', 'page')
                    expect(page.get_by_role('status')).to_contain_text('List updated')
                    self.assertEqual(embeddings, [])
                    self.assertEqual(len(lists), 1)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    for selector in ('.archive-primary-navigation a', '.sites-refresh', '.site-card'):
                        for height in page.locator(selector).evaluate_all('elements => elements.map(e => e.getBoundingClientRect().height)'):
                            self.assertGreaterEqual(height, 44)

                    fail_list = True
                    page.get_by_role('button', name='Refresh list').click()
                    expect(page.get_by_role('alert')).to_contain_text('previous list is still shown')
                    expect(page.locator('.site-card')).to_have_count(3)
                    fail_list = False
                    page.get_by_role('button', name='Refresh list').click()
                    expect(page.get_by_role('alert')).to_have_count(0)
                    link = page.get_by_role('link', name=f'Search captures from {domain}', exact=True)
                    if width <= 800:
                        link.tap()
                    else:
                        link.click()
                    scope = page.get_by_role('region', name='Site search scope')
                    expect(scope).to_contain_text(domain)
                    expect(page.get_by_role('heading', name=domain, exact=True)).to_be_visible()
                    expect(page.get_by_role('heading', name='Rediscover early EverQuest.', exact=True)).to_have_count(0)
                    expect(page.locator('.sui-result')).to_have_count(20)
                    check_scope()

                    search = page.locator('.sui-search-box__text-input')
                    search.fill('cleric')
                    search.fill('cleric healing')
                    with page.expect_response(lambda response: response.url.endswith('/_search')):
                        search.press('Enter')
                    check_scope()
                    self.assertTrue(searches[-1]['knn'])
                    with page.expect_response(lambda response: response.url.endswith('/_search')):
                        search.fill('"paladin spells"')
                        search.press('Enter')
                    check_scope()
                    self.assertNotIn('knn', searches[-1])
                    page.get_by_role('button', name='Next', exact=True).click()
                    expect(page.locator('.sui-result')).to_have_count(6)
                    expect(scope).to_contain_text(domain)
                    page.get_by_role('button', name='Previous', exact=True).click()
                    expect(page.locator('.sui-result')).to_have_count(20)
                    show_filters = page.get_by_role('button', name='Show Filters', exact=True)
                    if show_filters.is_visible():
                        show_filters.click()
                    page.locator('.sui-sorting .sui-select__control').click()
                    with page.expect_response(lambda response: response.url.endswith('/_search')):
                        page.get_by_role('option', name='Captured date (Descending)', exact=True).click()
                    check_scope()
                    save_filters = page.get_by_role('button', name='Save Filters', exact=True)
                    if save_filters.is_visible():
                        save_filters.click()

                    page.wait_for_url(lambda url: 'sort' in str(url))
                    focused_url = page.url
                    page.locator('.sui-result').first.get_by_role('link', name='Read document', exact=True).click()
                    expect(page.get_by_role('heading', name='Guild page 0', exact=True)).to_be_visible()
                    page.get_by_role('link', name='← Back to results', exact=True).click()
                    expect(scope).to_contain_text(domain)
                    page.reload(wait_until='networkidle')
                    check_scope()
                    expect(search).to_have_value('"paladin spells"')
                    self.assertEqual(page.url, focused_url)
                    with page.expect_response(lambda response: response.url.endswith('/_search')):
                        page.get_by_role('button', name='Search all sites', exact=True).click()
                    expect(scope).to_have_count(0)
                    self.assertNotIn(domain, domain_values([searches[-1].get('query'), searches[-1].get('post_filter')]))
                    expect(search).to_have_value('"paladin spells"')
                    page.wait_for_url(lambda url: 'domain_name' not in str(url))
                    with page.expect_response(lambda response: response.url.endswith('/_search')):
                        page.go_back(wait_until='networkidle')
                    expect(scope).to_contain_text(domain)
                    check_scope()
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_date_ranges_persist_across_results_searches_reload_and_history(self):
        for width, timezone in ((390, 'America/Los_Angeles'), (1280, 'Pacific/Auckland')):
            with self.subTest(width=width, timezone=timezone):
                context = self.browser.new_context(
                    viewport={'width': width, 'height': 950}, timezone_id=timezone,
                    is_mobile=width <= 800, has_touch=width <= 800,
                )
                try:
                    page = context.new_page()
                    requests, held, errors = [], [], []
                    delay = False
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    def archive(route):
                        if route.request.url.endswith('/_search'):
                            requests.append(route.request.post_data_json)
                            if delay:
                                held.append(route)
                                return
                        self.mock_elasticsearch(route)

                    page.route('**/elasticsearch/**', archive)
                    page.route('**/openai/v1/embeddings', lambda route: route.fulfill(
                        json={'data': [{'embedding': [0.1] * 768}]}))
                    page.goto(self.base_url, wait_until='networkidle')
                    facets = page.locator('.archive-date-facet')
                    fields = ('capture_date', 'llm_guessed_date')
                    expected_ranges = {}
                    month = page.evaluate("() => { const date = new Date(); return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`; }")
                    display_month = f'{month[5:]}/{month[:4]}'

                    def show_filters():
                        button = page.get_by_role('button', name='Show Filters', exact=True)
                        if button.is_visible() and not facets.first.is_visible():
                            button.click()

                    def hide_filters():
                        button = page.get_by_role('button', name='Save Filters', exact=True)
                        if button.is_visible():
                            button.click()

                    def choose(facet, index, day):
                        if width <= 800:
                            facet.locator('input').nth(index).click()
                        else:
                            facet.get_by_role('button', name='Choose date', exact=False).nth(index).click()
                        dialog = page.get_by_role('dialog')
                        expect(dialog).to_be_visible()
                        dialog.evaluate('dialog => Promise.all(dialog.getAnimations({subtree:true}).map(a => a.finished))')
                        page.get_by_role('gridcell', name=str(day), exact=True).click()
                        confirm = page.get_by_role('button', name='OK', exact=True)
                        if confirm.is_visible():
                            confirm.click()
                        expect(page.get_by_role('dialog', include_hidden=True)).to_have_count(0)

                    def ranges(value):
                        found = {}
                        if isinstance(value, dict):
                            found.update({k: v for k, v in value.get('range', {}).items() if k in fields})
                            for child in value.values():
                                found.update(ranges(child))
                        elif isinstance(value, list):
                            for child in value:
                                found.update(ranges(child))
                        return found

                    def check_request():
                        body = requests[-1]
                        self.assertEqual(ranges(body.get('query')), expected_ranges)
                        for branch in body.get('knn', []):
                            self.assertEqual(ranges(branch.get('filter')), expected_ranges)
                        if body.get('query', {}).get('bool', {}).get('should'):
                            self.assertEqual(body['query']['bool']['minimum_should_match'], 1)

                    def response():
                        return page.expect_response(lambda reply: reply.url.endswith('/_search') and reply.status == 200)

                    def apply(index, start=15):
                        with response():
                            facets.nth(index).get_by_role('button', name='Apply', exact=True).click()
                        expected_ranges[fields[index]] = {'gte': f'{month}-{start:02d}T00:00:00.000Z',
                                                          'lte': f'{month}-16T23:59:59.999Z'}
                        check_request()

                    show_filters()
                    for index in range(2):
                        choose(facets.nth(index), 0, 15)
                        choose(facets.nth(index), 1, 16)
                        apply(index)
                    hide_filters()

                    # Hold the result while a user edits the next range. Neither
                    # its response nor another search may replace that draft.
                    delay = True
                    with page.expect_request('**/elasticsearch/**/_search'):
                        page.locator('.sui-search-box__text-input').fill('"cleric"')
                        page.locator('.sui-search-box__text-input').press('Enter')
                    show_filters()
                    choose(facets.first, 0, 14)
                    expect(facets.first.locator('input').first).to_have_value(f'14/{display_month}')
                    self.assertTrue(held)
                    delay = False
                    for route in held:
                        self.mock_elasticsearch(route)
                    expect(page.get_by_role('progressbar')).to_have_count(0)
                    expect(facets.first.locator('input').first).to_have_value(f'14/{display_month}')
                    check_request()
                    hide_filters()

                    for query in ('"wizard"', 'wizard research', ''):
                        with response():
                            page.locator('.sui-search-box__text-input').fill(query)
                            page.locator('.sui-search-box__text-input').press('Enter')
                        check_request()
                        if query == 'wizard research':
                            self.assertTrue(requests[-1]['knn'])
                    show_filters()
                    expect(facets.first.locator('input').first).to_have_value(f'14/{display_month}')
                    apply(0, 14)
                    page.locator('.sui-sorting .sui-select__control').click()
                    with response():
                        page.get_by_role('option', name='Captured date (Descending)', exact=True).click()
                    check_request()
                    hide_filters()
                    page.wait_for_url(lambda url: f'{month}-14T00:00:00.000Z' in json.dumps(parse_qs(urlparse(str(url)).query)))
                    page.reload(wait_until='networkidle')
                    check_request()
                    show_filters()
                    expect(facets.first.locator('input').first).to_have_value(f'14/{display_month}')
                    expect(facets.nth(1).locator('input').last).to_have_value(f'16/{display_month}')

                    with response():
                        facets.first.get_by_role('button', name='Clear', exact=True).click()
                    original = expected_ranges.pop('capture_date')
                    check_request()
                    page.wait_for_url(lambda url: all(value != ['capture_date'] for key, value in
                        parse_qs(urlparse(str(url)).query).items()
                        if key.startswith('filters[') and key.endswith('[field]')))
                    with response():
                        page.go_back(wait_until='networkidle')
                    expected_ranges['capture_date'] = original
                    check_request()
                    expect(facets.first.locator('input').first).to_have_value(f'14/{display_month}')
                    with response():
                        page.go_forward(wait_until='networkidle')
                    expected_ranges.pop('capture_date')
                    check_request()
                    expect(facets.first.locator('input').first).to_have_value('')
                    with response():
                        facets.nth(1).get_by_role('button', name='Clear', exact=True).click()
                    expected_ranges.clear()
                    check_request()
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_results_per_page_keeps_its_selected_value_visible_on_mobile(self):
        facets = {field: {"buckets": []} for field in (
            "domain_name", "llm_content_flavour", "file_type", "mime_type",
            "mailing_list_name", "llm_tags"
        )}
        facets["last_indexed"] = {"buckets": {}}

        def archive(route):
            if route.request.url.endswith('/_count'):
                return route.fulfill(json={"count": 1})
            return route.fulfill(json={
                "hits": {"total": {"value": 10000, "relation": "gte"}, "hits": [{
                    "_id": "website/one", "_score": 1,
                    "_source": {"title": "One source", "url": "https://example.org/one"}
                }]},
                "aggregations": {"facet_bucket_all": {"doc_count": 10000, **facets}}
            })

        for width in (320, 390, 600, 768, 1280):
            with self.subTest(width=width):
                context = self.browser.new_context(
                    viewport={"width": width, "height": 900},
                    is_mobile=width <= 800, has_touch=width <= 800,
                )
                try:
                    page = context.new_page()
                    page.route('**/elasticsearch/**', archive)
                    page.goto(self.base_url, wait_until='networkidle')
                    expect(page.get_by_text('Showing sources 1–1 · 10,000+ matching captures')).to_be_visible()
                    per_page = page.locator('.sui-results-per-page')
                    selected = per_page.locator('.sui-select__single-value')

                    def assert_selected_value(value):
                        expect(selected).to_have_text(value)
                        # React Select can keep the text in the DOM while clipping it to zero width.
                        visible_width, text_width = selected.evaluate('''element => {
                            const range = document.createRange();
                            range.selectNodeContents(element);
                            return [element.getBoundingClientRect().width, range.getBoundingClientRect().width];
                        }''')
                        self.assertGreaterEqual(visible_width + 0.5, text_width)
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

                    assert_selected_value('20')
                    if width <= 650:
                        summary = page.locator('.archive-group-controls').bounding_box()
                        self.assertGreaterEqual(per_page.bounding_box()['y'], summary['y'] + summary['height'])
                    per_page.locator('.sui-select__control').click()
                    per_page.get_by_role('option', name='40').click()
                    assert_selected_value('40')
                finally:
                    context.close()

    def test_mcp_icon_and_connection_guide_work_on_mobile_and_desktop(self):
        for width in (320, 390, 1280):
            with self.subTest(width=width):
                context = self.browser.new_context(viewport={"width": width, "height": 900}, permissions=["clipboard-read", "clipboard-write"])
                try:
                    page = context.new_page()
                    page.route("**/elasticsearch/**", self.mock_elasticsearch)
                    page.goto(self.base_url, wait_until="networkidle")
                    link = page.get_by_role("link", name="Connect with MCP", exact=True)
                    expect(link).to_be_visible()
                    bounds = link.bounding_box()
                    self.assertGreaterEqual(bounds["x"], 0)
                    self.assertLessEqual(bounds["x"] + bounds["width"], width)
                    brand = page.get_by_role("link", name="EQ Archives home").bounding_box()
                    self.assertGreater(bounds["x"], brand["x"] + brand["width"])
                    self.assertLess(abs((bounds["y"] + bounds["height"] / 2) - (brand["y"] + brand["height"] / 2)), 2)
                    self.assertTrue(link.locator("img").evaluate("img => img.complete && img.naturalWidth > 0"))
                    link.click()
                    expect(page.get_by_role("heading", level=1)).to_have_text("Bring EverQuest history into your conversations.")
                    expect(page.get_by_role("heading", name="ChatGPT developer mode", exact=True)).to_be_visible()
                    expect(page.get_by_role("heading", name="Claude", exact=True)).to_be_visible()
                    expect(page.locator("code")).to_have_text("https://search.eqarchives.org/mcp")
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                    page.get_by_role("button", name="Copy MCP server URL").click()
                    expect(page.get_by_role("status")).to_have_text("MCP server URL copied.")
                    self.assertEqual(page.evaluate("navigator.clipboard.readText()"), "https://search.eqarchives.org/mcp")
                    page.get_by_role("link", name="EQ Archives", exact=True).click()
                    expect(page.locator(".sui-search-box__text-input")).to_be_visible()
                finally:
                    context.close()

    def test_lightweight_search_opens_the_reader_without_a_duplicate_preview(self):
        for width in (320, 390, 1280):
            with self.subTest(width=width):
                context = self.browser.new_context(viewport={"width": width, "height": 900})
                try:
                    page = context.new_page()
                    requests, documents, embeddings = [], [], []
                    source = {
                        "title": "Ancient Cyclops notes", "url": "https://example.org/source",
                        "llm_summary": "A useful archive summary. " * 30, "text_full": "# Preserved source\n\nThe complete original document.",
                        "text": [{"text_chunk": "Unneeded chunk", "vector": [1, 2]}],
                        "llm_summary_vector": [1, 2], "capture_date": "2000-01-01T00:00:00Z",
                        "domain_name": "example.org", "llm_tags": ["Cyclops"]
                    }
                    doc_id = "archives/source with spaces?#.txt"

                    def archive(route):
                        if route.request.url.endswith('/_count'):
                            return route.fulfill(json={"count": 1})
                        body = route.request.post_data_json
                        if 'ids' in body.get('query', {}):
                            documents.append(body)
                            self.assertEqual(body['size'], 1)
                            self.assertEqual(body['query']['ids']['values'], [doc_id])
                            self.assertIn('text_full', body['_source'])
                            self.assertIn('title', body['_source'])
                            self.assertNotIn('text', body['_source'])
                            return route.fulfill(json={"hits": {"hits": [{"_id": doc_id, "_source": {field: source[field] for field in body['_source'] if field in source}}]}})
                        requests.append(body)
                        selected = body['_source']
                        included = selected.get('includes', ['*'])
                        excluded = selected.get('excludes', [])
                        fields = {key: value for key, value in source.items()
                                  if any(fnmatch.fnmatchcase(key, item) for item in included)
                                  and not any(fnmatch.fnmatchcase(key, item) for item in excluded)}
                        self.assertNotIn('text_full', fields)
                        self.assertNotIn('text', fields)
                        self.assertNotIn('llm_summary_vector', fields)
                        self.assertEqual(body['highlight']['encoder'], 'html')
                        self.assertEqual(body['highlight']['fields']['text_full']['number_of_fragments'], 1)
                        self.assertTrue(all('inner_hits' not in query for query in body.get('knn', [])))
                        facets = {field: {"buckets": []} for field in (
                            "domain_name", "llm_content_flavour", "file_type", "mime_type", "mailing_list_name", "llm_tags"
                        )}
                        facets["domain_name"] = {"buckets": [{"key": "example.org", "doc_count": 1}]}
                        facets["last_indexed"] = {"buckets": {}}
                        route.fulfill(json={
                            "hits": {"total": {"value": 1, "relation": "eq"}, "hits": [{
                                "_id": doc_id, "_score": 1, "_source": fields,
                                "highlight": {"text_full": ["A <em>matching</em> archive passage"]}
                            }]}, "aggregations": {"facet_bucket_all": {"doc_count": 1, **facets}}
                        })

                    def embed(route):
                        embeddings.append(route.request.post_data_json)
                        route.fulfill(json={"data": [{"embedding": [0.1] * 768}]})

                    page.route('**/elasticsearch/**', archive)
                    page.route('**/openai/v1/embeddings', embed)
                    page.goto(self.base_url, wait_until='networkidle')
                    expect(page.get_by_role('link', name='Ancient Cyclops notes')).to_be_visible()
                    expect(page.get_by_text('A matching archive passage')).to_be_visible()
                    card = page.locator('.sui-result').first
                    summary = card.locator('details.result-summary')
                    self.assertFalse(summary.evaluate('element => element.open'))
                    self.assertLess(card.locator('.result-text-snippet').bounding_box()['y'], summary.bounding_box()['y'])
                    expect(page.get_by_role('button', name='Show detailed cards', exact=True)).to_have_count(0)
                    expect(page.get_by_role('button', name='Show compact cards', exact=True)).to_have_count(0)
                    summary.locator('summary').click()
                    self.assertTrue(summary.evaluate('element => element.open'))
                    summary.locator('summary').click()
                    self.assertFalse(summary.evaluate('element => element.open'))
                    self.assertEqual(documents, [])
                    expect(page.get_by_role('button', name='Preview Full Text', exact=True)).to_have_count(0)
                    reader = page.get_by_role('link', name='Read document', exact=True)
                    expect(reader).to_be_visible()
                    self.assertEqual(parse_qs(urlparse(reader.get_attribute('href')).query)['id'], [doc_id])
                    if width <= 650:
                        actions = page.locator('.archive-result-actions').bounding_box()
                        primary = reader.bounding_box()
                        self.assertAlmostEqual(primary['width'], actions['width'], delta=1)
                        self.assertGreaterEqual(page.get_by_role('button', name='Alternate Link', exact=True).bounding_box()['y'], primary['y'] + primary['height'])
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

                    def search(value):
                        with page.expect_response(lambda response: '/_search' in response.url and
                                                  value in str(response.request.post_data_json) and response.status == 200):
                            page.locator('.sui-search-box__text-input').fill(value)
                            page.locator('.sui-search-box__text-input').press('Enter')
                        expect(page.locator('.sui-search-box__text-input')).to_have_value(value)

                    search('ancient cyclops')
                    self.assertEqual(len(embeddings), 1)
                    self.assertEqual(embeddings[0]['input'], ['search_query: ancient cyclops'])
                    self.assertIn('knn', requests[-1])
                    self.assertEqual(len(requests[-1]['knn']), 3)
                    for branch in requests[-1]['knn']:
                        self.assertEqual((branch['k'], branch['num_candidates'], branch['boost']), (50, 250, 10))
                    search('"ancient cyclops"')
                    self.assertEqual(len(embeddings), 1)
                    self.assertNotIn('knn', requests[-1])
                    page.get_by_role('button', name='Advanced...', exact=True).click()
                    expect(page.get_by_role('checkbox', name='Enable semantic search')).to_be_checked()
                    for field, value in (('k', '50'), ('num_candidates', '250'), ('boost', '10')):
                        expect(page.get_by_label(field, exact=True)).to_have_value(value)
                    page.get_by_role('checkbox', name='Enable semantic search').uncheck()
                    search('cleric soloing')
                    self.assertEqual(len(embeddings), 1)
                    self.assertNotIn('knn', requests[-1])
                    show_filters = page.get_by_role('button', name='Show Filters', exact=True)
                    if show_filters.is_visible():
                        show_filters.click()
                    sorting = page.locator('.sui-sorting')
                    sorting.locator('.sui-select__control').click()
                    with page.expect_response('**/elasticsearch/**/_search'):
                        page.get_by_role('option', name='Captured date (Descending)', exact=True).click()
                    source_filter = page.locator('.sui-multi-checkbox-facet').first.get_by_role('checkbox').first
                    with page.expect_response('**/elasticsearch/**/_search'):
                        source_filter.check()
                    self.assertTrue(source_filter.is_checked())
                    page.wait_for_url(lambda url: 'sort-field=' in str(url) and 'filters' in str(url) and 'cleric' in str(url))
                    save_filters = page.get_by_role('button', name='Save Filters', exact=True)
                    if save_filters.is_visible():
                        save_filters.click()
                    self.assertEqual(documents, [])
                    results_url = page.url
                    results_path = urlparse(results_url).path + '?' + urlparse(results_url).query
                    self.assertIn('sort-field=', results_url)
                    self.assertIn('filters', results_url)
                    reader.hover()
                    self.assertEqual(parse_qs(urlparse(reader.get_attribute('href')).query)['return'], [results_path])
                    reader.click()
                    expect(page.get_by_role('heading', name='Preserved source', exact=True)).to_be_visible()
                    expect(page.get_by_text('The complete original document.', exact=True)).to_be_visible()
                    self.assertEqual(len(documents), 1)
                    page.reload(wait_until='networkidle')
                    back = page.get_by_role('link', name='← Back to results', exact=True)
                    expect(back).to_have_attribute('href', results_path)
                    back.click()
                    expect(page.get_by_role('link', name='Ancient Cyclops notes')).to_be_visible()
                    self.assertEqual(page.url, results_url)
                    expect(page.locator('.sui-search-box__text-input')).to_have_value('cleric soloing')
                    show_filters = page.get_by_role('button', name='Show Filters', exact=True)
                    if show_filters.is_visible():
                        show_filters.click()
                    expect(page.locator('.sui-multi-checkbox-facet').first.get_by_role('checkbox').first).to_be_checked()
                    expect(page.locator('.sui-sorting .sui-select__single-value')).to_have_text('Captured date (Descending)')
                    expect(page.get_by_role('dialog')).to_have_count(0)
                    self.assertEqual(len(documents), 2)
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                finally:
                    context.close()

    def test_repeated_captures_group_across_pages_and_keep_individual_previews(self):
        for width in (390, 1280):
            with self.subTest(width=width):
                context = self.browser.new_context(viewport={"width": width, "height": 950})
                try:
                    page = context.new_page()
                    previews, searches, histories = [], [], []

                    def hit(path, day, title):
                        stamp = (datetime(2000, 1, 1) + timedelta(days=day)).strftime('%Y%m%d%H%M%S')
                        return {"_id": f"websites/example.org/{stamp}/{path}", "_score": 1, "_source": {
                            "title": title, "url": f"https://web.archive.org/web/{stamp}/http://example.org/{path}",
                            "capture_date": (datetime(2000, 1, 1) + timedelta(days=day)).isoformat(),
                            "llm_summary": "Archived source summary", "domain_name": "example.org"
                        }}

                    duplicate = [hit('thread?id=1', day, 'Repeated page') for day in range(60)]
                    records = duplicate[:50] + [hit(f'thread?id={i}', 1, f'Distinct page {i}') for i in range(2, 26)] + duplicate[50:]
                    # Duplicate captures straddle raw batches; query-string pages remain distinct.
                    facets = {field: {"buckets": []} for field in (
                        "domain_name", "llm_content_flavour", "file_type", "mime_type", "mailing_list_name", "llm_tags"
                    )}
                    facets["last_indexed"] = {"buckets": {}}

                    def archive(route):
                        if route.request.url.endswith('/_count'):
                            return route.fulfill(json={"count": len(records)})
                        body = route.request.post_data_json
                        if 'ids' in body.get('query', {}):
                            previews.append(body)
                            requested = body['query']['ids']['values'][0]
                            return route.fulfill(json={"hits": {"hits": [{"_id": requested, "_source": {"text_full": '# Selected capture\n\nExact source: ' + requested}}]}})
                        if 'preference=archive-captures' in route.request.url:
                            histories.append(body)
                            self.assertNotIn('text_full', body['_source'])
                            return route.fulfill(json={"hits": {"total": {"value": 2, "relation": "eq"}, "hits": [duplicate[59], duplicate[0]]}})
                        searches.append(body)
                        start = body.get('from', 0)
                        batch = records[start:start + body['size']]
                        route.fulfill(json={"hits": {"total": {"value": len(records), "relation": "eq"}, "hits": batch},
                                            "aggregations": {"facet_bucket_all": {"doc_count": len(records), **facets}}})

                    page.route('**/elasticsearch/**', archive)
                    page.goto(self.base_url, wait_until='networkidle')
                    expect(page.locator('.sui-result')).to_have_count(20)
                    expect(page.get_by_role('link', name='Repeated page', exact=True)).to_have_count(1)
                    expect(page.get_by_role('link', name='Distinct page 2', exact=True)).to_have_count(1)
                    expect(page.get_by_text('Showing sources 1–20 · 84 matching captures')).to_be_visible()
                    self.assertEqual(histories, [])
                    self.assertEqual(previews, [])
                    page.get_by_role('button', name='Next', exact=True).click()
                    expect(page.locator('.sui-result')).to_have_count(5)
                    expect(page.get_by_role('link', name='Repeated page', exact=True)).to_have_count(0)
                    expect(page.get_by_role('button', name='Next', exact=True)).to_be_disabled()
                    page.get_by_role('button', name='Previous', exact=True).click()
                    expect(page.get_by_role('link', name='Repeated page', exact=True)).to_have_count(1)
                    first = page.locator('.sui-result').first
                    first.get_by_role('button', name='View captures', exact=True).click()
                    expect(first.get_by_text('2000-02-29', exact=True)).to_be_visible()
                    expect(first.get_by_text('2000-01-01', exact=True)).to_be_visible()
                    first.get_by_role('button', name='Preview capture from 2000-01-01').click()
                    expect(page.get_by_role('heading', name='Selected capture', exact=True)).to_be_visible()
                    self.assertEqual(previews[-1]['query']['ids']['values'], [duplicate[0]['_id']])
                    expect(page.get_by_role('heading', name='Capture · 2000-01-01')).to_be_visible()
                    page.get_by_role('button', name='Close', exact=True).click()
                    expect(page.get_by_role('dialog')).to_have_count(0)
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    for link in first.locator('.archive-capture-actions a').all():
                        bounds = link.bounding_box()
                        self.assertLessEqual(bounds['x'] + bounds['width'], width)
                    first.get_by_role('button', name='Hide captures (2)', exact=True).click()
                    first.get_by_role('button', name='View captures (2)', exact=True).click()
                    self.assertEqual(len(histories), 1)
                    page.get_by_role('checkbox', name='Group repeated captures').uncheck()
                    expect(page.get_by_role('link', name='Repeated page', exact=True)).to_have_count(20)
                    page.get_by_role('checkbox', name='Group repeated captures').check()
                    expect(page.get_by_role('link', name='Repeated page', exact=True)).to_have_count(1)
                    expect(page.get_by_text('Showing sources 1–20 · 84 matching captures')).to_be_visible()
                    self.assertTrue(all(body.get('from', 0) + body['size'] <= 1000 for body in searches))
                    # An old ungrouped bookmark must not strand readers on an empty page.
                    page.goto(self.base_url + '/?current=n_50_n', wait_until='networkidle')
                    expect(page.locator('.sui-result')).to_have_count(5)
                    expect(page.get_by_text('Page 2', exact=True)).to_be_visible()
                    expect(page.get_by_role('button', name='Next', exact=True)).to_be_disabled()
                    page.wait_for_url(lambda url: 'current=n_50_n' in str(url) and 'size=' in str(url))
                    page_two = page.url
                    page.locator('.sui-result').first.get_by_role('link', name='Read document', exact=True).click()
                    expect(page.get_by_role('link', name='← Back to results', exact=True)).to_have_attribute('href', urlparse(page_two).path + '?' + urlparse(page_two).query)
                    page.get_by_role('link', name='← Back to results', exact=True).click()
                    expect(page.get_by_text('Page 2', exact=True)).to_be_visible()
                    self.assertEqual(page.url, page_two)
                finally:
                    context.close()

    def test_document_reader_and_capture_comparisons(self):
        for width in (320, 390, 1280):
            with self.subTest(width=width):
                context = self.browser.new_context(viewport={"width": width, "height": 950}, permissions=["clipboard-read", "clipboard-write"])
                try:
                    page = context.new_page()
                    errors, requests, embeddings = [], [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    first_id = 'websites/example.org/20000101000000/guide?a=1&b=2'
                    second_id = 'websites/example.org/20010101000000/guide?a=1&b=2'
                    text = '# Preserved guide\n\nAncient cyclops. Ancient cyclops.\n\n[Related page](../spells)\n\n![Historical image](picture.png)\n\n[![Battle screenshot](battle.jpg)](full-battle.jpg)\n\n| Weapon | Damage |\n| --- | --- |\n| Sword | 42 |\n\nOld advice\n'
                    later = text.replace('Old advice', 'New advice')
                    records = {
                        first_id: {"title": "Cyclops research guide", "url": "https://web.archive.org/web/20000101000000/http://example.org/guide?a=1&b=2", "capture_date": "2000-01-01T00:00:00Z", "llm_guessed_date": "1999-01-01", "domain_name": "example.org", "text_full": text, "llm_image_text_full": "Old OCR"},
                        second_id: {"title": "Cyclops research guide, revised", "url": "https://web.archive.org/web/20010101000000/http://example.org/guide?a=1&b=2", "capture_date": "2001-01-01T00:00:00Z", "domain_name": "example.org", "text_full": later, "llm_image_text_full": "New OCR"},
                    }
                    facets = {field: {"buckets": []} for field in (
                        "domain_name", "llm_content_flavour", "file_type", "mime_type", "mailing_list_name", "llm_tags"
                    )}
                    facets["last_indexed"] = {"buckets": {}}

                    def archive(route):
                        if route.request.url.endswith('/_count'):
                            return route.fulfill(json={"count": 2})
                        body = route.request.post_data_json
                        requests.append(body)
                        if 'ids' in body.get('query', {}):
                            selected = body['query']['ids']['values'][0]
                            self.assertEqual(body['size'], 1)
                            self.assertNotIn('llm_summary', body['_source'])
                            self.assertNotIn('text', body['_source'])
                            return route.fulfill(json={"hits": {"hits": [{"_id": selected, "_source": records[selected]}]}})
                        chosen = [second_id, first_id] if 'preference=archive-captures' in route.request.url else [first_id]
                        hits = [{"_id": selected, "_source": {field: value for field, value in records[selected].items() if field not in ('text_full', 'llm_image_text_full')}, "_score": 1} for selected in chosen]
                        route.fulfill(json={"hits": {"total": {"value": len(hits), "relation": "eq"}, "hits": hits}, "aggregations": {"facet_bucket_all": {"doc_count": 2, **facets}}})

                    page.route('**/elasticsearch/**', archive)
                    page.route('**/openai/v1/embeddings', lambda route: (embeddings.append(True), route.fulfill(json={"data": [{"embedding": [0.1] * 768}]})))
                    page.goto(self.base_url + '/?' + urlencode({'q': '"ancient cyclops"'}), wait_until='networkidle')
                    link = page.get_by_role('link', name='Read document', exact=True)
                    expect(link).to_be_visible()
                    page.wait_for_url(lambda url: 'q=' in str(url) and 'size=' in str(url))
                    results_url = page.url
                    self.assertEqual(parse_qs(urlparse(link.get_attribute('href')).query)['find'], ['ancient cyclops'])
                    requests.clear()
                    link.click()
                    expect(page.get_by_role('heading', name='Cyclops research guide', exact=True)).to_be_visible()
                    expect(page.get_by_role('heading', name='Preserved guide', exact=True)).to_be_visible()
                    expect(page.get_by_text('1 of 2 matches', exact=True)).to_be_visible()
                    page.get_by_role('link', name='Jump to text', exact=True).click()
                    expect(page.get_by_role('searchbox', name='Find in document')).to_be_visible()
                    expect(page.locator('.reader-body img')).to_have_count(0)
                    expect(page.locator('.reader-body a a')).to_have_count(0)
                    expect(page.locator('.reader-body table')).to_have_count(1)
                    expect(page.get_by_role('link', name='[Image: Battle screenshot]', exact=True)).to_have_attribute('href', 'https://web.archive.org/web/20000101000000/http://example.org/full-battle.jpg')
                    expect(page.get_by_role('link', name='Related page')).to_have_attribute('href', 'https://web.archive.org/web/20000101000000/http://example.org/spells')
                    page.get_by_role('button', name='Next match', exact=True).click()
                    expect(page.get_by_text('2 of 2 matches', exact=True)).to_be_visible()
                    self.assertEqual(page.locator('mark.reader-match-active').count(), 1)
                    search = page.get_by_role('searchbox', name='Find in document')
                    search.fill('wrong')
                    search.fill('Old advice')
                    expect(search).to_have_value('Old advice')
                    expect(page.get_by_text('1 of 1 matches', exact=True)).to_be_visible()
                    page.get_by_role('checkbox', name='Source text', exact=True).check()
                    self.assertEqual(page.locator('.reader-body pre').text_content(), text)
                    page.get_by_role('button', name='Copy citation', exact=True).click()
                    expect(page.get_by_text('Copied.', exact=True)).to_be_visible()
                    self.assertIn('Captured 2000-01-01T00:00:00Z (archive timestamp)', page.evaluate('navigator.clipboard.readText()'))
                    with page.expect_download() as download:
                        page.get_by_role('button', name='Download text', exact=True).click()
                    self.assertEqual(Path(download.value.path()).read_bytes(), text.encode())
                    page.get_by_role('combobox', name='Text to view').select_option('ocr')
                    expect(page.get_by_text('Old OCR', exact=True)).to_be_visible()
                    expect(page.get_by_text('Image transcription (OCR) is model-generated and may contain errors. Check the original images when citing it.')).to_be_visible()
                    self.assertTrue(all('ids' in request.get('query', {}) for request in requests), requests)
                    self.assertEqual(embeddings, [])
                    page.get_by_role('button', name='View captures', exact=True).click()
                    comparison = page.get_by_role('link', name='Compare capture from 2001-01-01 with selected capture', exact=True)
                    expect(comparison).to_be_visible()
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    comparison.click()
                    expect(page.get_by_role('heading', name='Compare captures', exact=True)).to_be_visible()
                    expect(page.get_by_text('1 added · 1 removed lines', exact=True)).to_be_visible()
                    page.get_by_role('link', name='Jump to changes', exact=True).click()
                    expect(page.locator('.reader-removed pre')).to_have_text('Old advice\n')
                    expect(page.locator('.reader-added pre')).to_have_text('New advice\n')
                    page.get_by_role('button', name='Next change', exact=True).click()
                    expect(page.locator('.reader-removed')).to_be_focused()
                    page.get_by_role('link', name='Swap captures', exact=True).click()
                    expect(page.locator('.reader-removed pre')).to_have_text('New advice\n')
                    expect(page.locator('.reader-added pre')).to_have_text('Old advice\n')
                    page.reload(wait_until='networkidle')
                    expect(page.get_by_role('heading', name='Compare captures', exact=True)).to_be_visible()
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    page.get_by_role('link', name='Back to document', exact=True).click()
                    expect(page.get_by_role('heading', name='Cyclops research guide, revised', exact=True)).to_be_visible()
                    page.get_by_role('link', name='← Back to results', exact=True).click()
                    expect(page.get_by_role('link', name='Cyclops research guide', exact=True)).to_be_visible()
                    self.assertEqual(page.url, results_url)
                    expect(page.locator('.sui-search-box__text-input')).to_have_value('"ancient cyclops"')
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_mcp_guide_handles_blocked_clipboard_and_legacy_link(self):
        context = self.browser.new_context()
        try:
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: {writeText: () => Promise.reject(new Error('blocked'))}})")
            page.goto(self.base_url + "/chatgpt.html")
            expect(page).to_have_url(self.base_url + "/mcp.html")
            page.get_by_role("button", name="Copy MCP server URL").click()
            expect(page.get_by_role("status")).to_have_text("Select and copy the server URL above.")
        finally:
            context.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
