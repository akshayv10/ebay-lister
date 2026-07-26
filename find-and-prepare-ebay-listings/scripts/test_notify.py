#!/usr/bin/env python3
"""Offline tests for notification rendering."""

from __future__ import annotations

import daily_run
import notify


def test_sheet_sync_success_is_in_both_bodies() -> None:
    _, text, html = notify.compose({
        "date": "2026-07-24",
        "status": "listed",
        "niche": "Test",
        "listed_count": 2,
        "products": [],
        "sheet_sync": {"status": "synced", "written": 2, "queued": 0, "error": ""},
    })
    assert "Google Sheets: synced (written 2, queued 0)" in text
    assert "Google Sheets: synced (written 2, queued 0)" in html
    assert "Open Auto Lister sheet" in html


def test_sheet_sync_queue_error_is_visible() -> None:
    _, text, html = notify.compose({
        "date": "2026-07-24",
        "status": "listed",
        "niche": "Test",
        "listed_count": 1,
        "products": [],
        "sheet_sync": {
            "status": "queued",
            "written": 0,
            "queued": 1,
            "error": "temporary outage",
        },
    })
    assert "Sync error: temporary outage" in text
    assert "Sync error: temporary outage" in html


def test_ebay_price_shown_when_listed() -> None:
    _, text, html = notify.compose({
        "date": "2026-07-26",
        "status": "listed",
        "niche": "Test",
        "listed_count": 1,
        "products": [{
            "title": "Test Product",
            "aliexpress_url": "https://aliexpress.com/item/1.html",
            "ebay_url": "https://www.ebay.com/itm/123",
            "price": "12.99",
            "ebay_price": "24.99",
            "listing_id": "123",
        }],
    })
    assert "eBay price: USD 24.99" in text
    assert "USD 24.99" in html
    assert "eBay Price" in html


def test_ebay_price_absent_for_dry_run_product() -> None:
    _, text, html = notify.compose({
        "date": "2026-07-26",
        "status": "partial",
        "niche": "Test",
        "listed_count": 0,
        "products": [{
            "title": "Test Product",
            "aliexpress_url": "https://aliexpress.com/item/1.html",
            "price": "12.99",
            "reason": "dry run",
        }],
    })
    assert "eBay price:" not in text


def test_listed_summaries_reads_expected_ebay_price() -> None:
    products = [{
        "product_id": "1",
        "listing_title": "Test Product",
        "aliexpress_url": "https://aliexpress.com/item/1.html",
        "ebay_url": "https://www.ebay.com/itm/123",
        "listing_id": "123",
        "selected_variants": [{
            "visible_item_price": "12.99",
            "expected_ebay_price": "24.99",
        }],
    }]
    summaries = daily_run.listed_summaries(products)
    assert summaries[0]["price"] == "12.99"
    assert summaries[0]["ebay_price"] == "24.99"


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
