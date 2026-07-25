#!/usr/bin/env python3
"""Offline tests for independent reviewed preparation and publication."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from daily_history import load_history
from ebay_common import EbayError, UnknownOutcome, read_json, write_json
from ebay_listing import prepare_independent, publish_independent
from listing_job import initialize_result, normalize_source, record_prepared


def source(product_id: str, run_id: str = "frp-20260725-test") -> dict:
    return {
        "run_id": f"{run_id}-product-{product_id}",
        "local_calendar_date": "2026-07-25",
        "assigned_niche": "Smartphone Accessories",
        "product_id": product_id,
        "aliexpress_url": f"https://www.aliexpress.us/item/{product_id}.html",
        "source_title": f"Source {product_id}",
        "functional_fingerprint": f"function {product_id}",
        "verified_brand": "Unbranded",
        "listing_title": f"USB C Accessory {product_id[-4:]}",
        "listing_description": "Factual verified product description.",
        "condition": "NEW",
        "category_query": "USB C accessory",
        "aspects": {"Brand": ["Unbranded"], "Type": ["Accessory"]},
        "source_images": ["https://example.com/one.jpg"],
        "selected_variants": [
            {
                "id": f"sku-{product_id}",
                "options": {},
                "visible_item_price": "17.25",
                "delivered_total": "18.40",
                "quantity": 1,
            }
        ],
    }


def prepared_api(normalized: dict, suffix: str) -> dict:
    return {
        "environment": "production",
        "marketplace_id": "EBAY_US",
        "merchant_location_key": "irvine-92618",
        "payment_policy_id": "pay",
        "return_policy_id": "return",
        "fulfillment_policy_id": "ship",
        "campaign_id": "campaign",
        "promoted_rate_percent": "10.0",
        "category_id": "123",
        "required_aspects_complete": True,
        "normalized_aspects": normalized["aspects"],
        "eps_image_urls": ["https://i.ebayimg.com/images/g/test/s-l1600.jpg"],
        "image_import_failures": [],
        "inventory_items": [{"sku": normalized["selected_variants"][0]["sku"], "readback": {}}],
        "inventory_item_group": None,
        "offers": [
            {
                "sku": normalized["selected_variants"][0]["sku"],
                "offer_id": f"offer-{suffix}",
                "published": False,
                "readback": {"sku": normalized["selected_variants"][0]["sku"]},
            }
        ],
        "listing_fees": {},
    }


def prepared_product(product_id: str, suffix: str) -> dict:
    normalized = normalize_source(source(product_id))
    return {
        **normalized,
        "status": "api_prepared",
        "published": False,
        "publish_allowed": False,
        "api": prepared_api(normalized, suffix),
    }


def setup_run(root: Path, products: list[dict]) -> None:
    write_json(
        root / "run-result.json",
        {
            "mode": "ebay_api_handoff",
            "status": "api_prepared" if len([p for p in products if p["status"] == "api_prepared"]) == 2
            else "api_prepared_partial",
            "run_id": root.name,
            "product_count": 2,
            "prepared_count": len([p for p in products if p["status"] == "api_prepared"]),
            "published": False,
            "publish_allowed": False,
            "products": products,
        },
    )


def config() -> dict:
    return {"campaign_id": "campaign", "merchant_location_key": "irvine-92618"}


def test_prepare_keeps_one_offer_when_sibling_blocks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "frp-20260725-test"
        for index, product_id in enumerate(("1005000000000001", "1005000000000002"), 1):
            product_dir = root / f"product-{index}"
            product_dir.mkdir(parents=True)
            write_json(product_dir / "source.json", source(product_id))

        def prepare_side_effect(_client, _config, result_path):
            value = read_json(result_path)
            if value["product_id"].endswith("2"):
                raise EbayError("taxonomy blocked")
            return record_prepared(
                result_path,
                prepared_api(normalize_source(value), "one"),
            )

        with patch("ebay_listing.require_setup", return_value=config()), \
             patch("ebay_listing.prepare_product", side_effect=prepare_side_effect):
            result = prepare_independent(root, object())
        assert result["status"] == "api_prepared_partial"
        assert result["prepared_count"] == 1
        assert [item["status"] for item in result["products"]] == ["api_prepared", "blocked"]


def test_publish_keeps_successful_sibling_live() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "frp-20260725-test"
        root.mkdir()
        setup_run(
            root,
            [
                prepared_product("1005000000000001", "one"),
                prepared_product("1005000000000002", "two"),
            ],
        )
        history = Path(directory) / "history.jsonl"
        with patch("ebay_listing.require_setup", return_value=config()), \
             patch("ebay_listing._validate_reviewed_product"), \
             patch(
                 "ebay_listing.publish_product",
                 side_effect=[
                     {"listing_id": "123456789001", "ebay_url": "https://www.ebay.com/itm/123456789001"},
                     EbayError("second failed"),
                 ],
             ), \
             patch("ebay_listing.promote", return_value="ad-one"), \
             patch("ebay_listing.published_listing_id", return_value="123456789001"):
            result = publish_independent(root, root.name, object(), history)
        assert result["status"] == "partial"
        assert result["listed_count"] == 1
        assert result["products"][0]["status"] == "live"
        assert result["products"][1]["status"] == "publish_failed"
        records = load_history(history)
        assert len(records) == 1 and records[0]["ebay_item_number"] == "123456789001"


def test_promotion_failure_withdraws_only_failed_product() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "frp-20260725-test"
        root.mkdir()
        setup_run(
            root,
            [
                prepared_product("1005000000000001", "one"),
                prepared_product("1005000000000002", "two"),
            ],
        )
        with patch("ebay_listing.require_setup", return_value=config()), \
             patch("ebay_listing._validate_reviewed_product"), \
             patch(
                 "ebay_listing.publish_product",
                 side_effect=[
                     {"listing_id": "123456789001", "ebay_url": "https://www.ebay.com/itm/123456789001"},
                     {"listing_id": "123456789002", "ebay_url": "https://www.ebay.com/itm/123456789002"},
                 ],
             ), \
             patch("ebay_listing.promote", side_effect=["ad-one", EbayError("promotion failed")]), \
             patch("ebay_listing.published_listing_id", return_value="123456789001"), \
             patch("ebay_listing._recover_ad_id", return_value=("", [])), \
             patch("ebay_listing.rollback", return_value=[]) as rollback_mock:
            result = publish_independent(root, root.name, object(), Path(directory) / "history.jsonl")
        assert result["status"] == "partial"
        assert result["products"][0]["status"] == "live"
        assert result["products"][1]["status"] == "publish_failed"
        rollback_mock.assert_called_once()
        assert rollback_mock.call_args.args[2][0]["listing_id"] == "123456789002"


def test_unknown_outcome_marks_only_affected_product_for_reconciliation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "frp-20260725-test"
        root.mkdir()
        setup_run(
            root,
            [
                prepared_product("1005000000000001", "one"),
                prepared_product("1005000000000002", "two"),
            ],
        )
        with patch("ebay_listing.require_setup", return_value=config()), \
             patch("ebay_listing._validate_reviewed_product"), \
             patch(
                 "ebay_listing.publish_product",
                 side_effect=[
                     {"listing_id": "123456789001", "ebay_url": "https://www.ebay.com/itm/123456789001"},
                     UnknownOutcome("timed out"),
                 ],
             ), \
             patch("ebay_listing.promote", return_value="ad-one"), \
             patch("ebay_listing.published_listing_id", return_value="123456789001"):
            result = publish_independent(root, root.name, object(), Path(directory) / "history.jsonl")
        assert result["status"] == "reconciliation_required"
        assert result["products"][0]["status"] == "live"
        assert result["products"][1]["status"] == "reconciliation_required"


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
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
