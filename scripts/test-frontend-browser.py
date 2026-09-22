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
        for width in (390, 1280):
            with self.subTest(width=width):
                context = self.browser.new_context(viewport={"width": width, "height": 900})
                try:
                    page = context.new_page()
                    requests, documents, embeddings = [], [], []
                    source = {
                        "title": "Ancient Cyclops notes", "url": "https://example.org/source",
                        "llm_summary": "A useful archive summary", "text_full": "# Preserved source\n\nThe complete original document.",
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
                    search('"ancient cyclops"')
                    self.assertEqual(len(embeddings), 1)
                    self.assertNotIn('knn', requests[-1])
                    page.get_by_role('button', name='Advanced...', exact=True).click()
                    page.get_by_role('checkbox', name='Enable semantic search').uncheck()
                    search('cleric soloing')
                    self.assertEqual(len(embeddings), 1)
                    self.assertNotIn('knn', requests[-1])
                    self.assertEqual(documents, [])
                    reader.click()
                    expect(page.get_by_role('heading', name='Preserved source', exact=True)).to_be_visible()
                    expect(page.get_by_text('The complete original document.', exact=True)).to_be_visible()
                    expect(page.get_by_role('dialog')).to_have_count(0)
                    self.assertEqual(len(documents), 1)
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
                    text = '# Preserved guide\n\nAncient cyclops. Ancient cyclops.\n\n[Related page](../spells)\n\n![Historical image](picture.png)\n\nOld advice\n'
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
                    self.assertEqual(parse_qs(urlparse(link.get_attribute('href')).query)['find'], ['ancient cyclops'])
                    requests.clear()
                    link.click()
                    expect(page.get_by_role('heading', name='Cyclops research guide', exact=True)).to_be_visible()
                    expect(page.get_by_role('heading', name='Preserved guide', exact=True)).to_be_visible()
                    expect(page.get_by_text('1 of 2 matches', exact=True)).to_be_visible()
                    page.get_by_role('link', name='Jump to text', exact=True).click()
                    expect(page.get_by_role('searchbox', name='Find in document')).to_be_visible()
                    expect(page.locator('.reader-body img')).to_have_count(0)
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
