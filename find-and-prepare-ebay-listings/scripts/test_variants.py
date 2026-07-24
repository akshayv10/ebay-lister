#!/usr/bin/env python3
"""Offline tests for AliExpress variant parsing, axis selection, and the eBay round-trip."""

from __future__ import annotations

from decimal import Decimal

import ali_api
from listing_job import normalize_source

# A ds.product.get-shaped result: 3 colours (one under $15) plus a logistics axis.
DETAIL = {
    "ae_item_base_info_dto": {
        "product_id": "1005006000000123",
        "subject": "RC Stunt Car 4WD Remote Control Drift Buggy",
        "avg_evaluation_rating": "4.8",
        "evaluation_count": "300",
        "sales_count": "900",
    },
    "ae_item_sku_info_dtos": {
        "ae_item_sku_info_d_t_o": [
            {"sku_id": "1001", "sku_attr": "14:200004889#Black;200007763:201336100#China",
             "offer_sale_price": "19.99", "sku_image": "https://x/black.jpg", "sku_available_stock": "50"},
            {"sku_id": "1002", "sku_attr": "14:200004890#Red;200007763:201336100#China",
             "offer_sale_price": "21.50", "sku_image": "https://x/red.jpg", "sku_available_stock": "30"},
            {"sku_id": "1003", "sku_attr": "14:200004891#Blue;200007763:201336100#China",
             "offer_sale_price": "22.75", "sku_image": "https://x/blue.jpg", "sku_available_stock": "10"},
            {"sku_id": "1004", "sku_attr": "14:200004892#Green;200007763:201336100#China",
             "offer_sale_price": "9.99", "sku_image": "https://x/green.jpg", "sku_available_stock": "99"},
        ]
    },
    "ae_item_sku_property_dtos": {
        "property": [
            {"sku_property_id": "14", "sku_property_name": "Color"},
            {"sku_property_id": "200007763", "sku_property_name": "Ships From"},
        ]
    },
    "ae_multimedia_info_dto": {"image_urls": "https://x/a.jpg;https://x/b.jpg"},
}


def test_parse_sku_attr_maps_ids_to_names() -> None:
    names = ali_api._sku_property_names(DETAIL)
    options = ali_api.parse_sku_attr("14:200004889#Black;200007763:201336100#China", names)
    assert options == {"Color": "Black", "Ships From": "China"}


def test_parse_variants_reads_price_image_stock() -> None:
    variants = ali_api.parse_variants(DETAIL)
    assert len(variants) == 4
    first = variants[0]
    assert first["price"] == Decimal("19.99")
    assert first["image"] == "https://x/black.jpg"
    assert first["stock"] == 50


def test_axis_selection_ignores_logistics_and_drops_cheap_variants() -> None:
    axis, chosen = ali_api.select_variants(ali_api.parse_variants(DETAIL))
    assert axis == "Color"  # not "Ships From"
    values = {v["options"]["Color"] for v in chosen}
    assert values == {"Black", "Red", "Blue"}  # Green ($9.99) dropped, below MIN_PRICE_USD
    assert len(chosen) <= ali_api.MAX_VARIANTS


def test_single_value_axis_yields_no_variants() -> None:
    detail = {
        "ae_item_base_info_dto": {"product_id": "1", "subject": "x"},
        "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
            {"sku_id": "1", "sku_attr": "14:1#Black", "offer_sale_price": "20.00"},
        ]},
        "ae_item_sku_property_dtos": {"property": [{"sku_property_id": "14", "sku_property_name": "Color"}]},
    }
    axis, chosen = ali_api.select_variants(ali_api.parse_variants(detail))
    assert axis == "" and chosen == []  # falls back to a single-variation listing


def test_multi_variant_source_normalizes_with_distinct_prices() -> None:
    _, chosen = ali_api.select_variants(ali_api.parse_variants(DETAIL))
    records = []
    for variant in chosen:
        value = variant["options"]["Color"]
        records.append({
            "id": value.lower(), "options": {"Color": value},
            "visible_item_price": f"{variant['price']:.2f}",
            "delivered_total": f"{variant['price']:.2f}",
            "quantity": 1, "image": variant["image"],
        })
    source = {
        "run_id": "r-product-1005006000000123", "local_calendar_date": "2026-07-24",
        "assigned_niche": "Hobbyist & Interactive Toys", "product_id": "1005006000000123",
        "aliexpress_url": "https://www.aliexpress.us/item/1005006000000123.html",
        "source_title": "RC Stunt Car", "functional_fingerprint": "rc stunt car",
        "verified_brand": "Unbranded", "listing_title": "RC Stunt Car 4WD Drift Buggy",
        "listing_description": "<p>Fast RC car.</p>", "condition": "NEW",
        "category_query": "RC Car", "aspects": {"Brand": ["Unbranded"], "MPN": ["N/A"]},
        "source_images": ["https://x/a.jpg", "https://x/b.jpg"],
        "selected_variants": records,
    }
    normalized = normalize_source(source)
    variants = normalized["selected_variants"]
    assert len(variants) == 3
    assert normalized["inventory_item_group_key"].startswith("ALI-GROUP-")
    assert len({v["sku"] for v in variants}) == 3           # distinct SKUs
    assert len({v["expected_ebay_price"] for v in variants}) == 3  # pricing rule per variant
    assert all(v["image"].startswith("https://") for v in variants)  # per-variant photo kept


def _run_all() -> int:
    tests = [v for n, v in sorted(globals().items()) if n.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
