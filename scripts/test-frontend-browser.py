#!/usr/bin/env python3
"""Browser regressions against the built frontend, with isolated API fixtures."""

import os
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
