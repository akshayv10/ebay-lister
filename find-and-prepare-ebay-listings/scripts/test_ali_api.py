#!/usr/bin/env python3
"""Offline tests for the AliExpress Dropshipping (DS) sourcing mapping and gates.
Never hits the network (fixture mode)."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import ali_api
from listing_job import normalize_source

FIXTURE = Path(__file__).with_name("fixtures") / "ali_sample.json"


def _details() -> list[dict]:
    os.environ["ALI_API_FIXTURE"] = str(FIXTURE)
    return ali_api.discover("anything", 1)


def test_flatten_reads_ds_dtos() -> None:
    stand = _details()[0]
    flat = ali_api.flatten_detail(stand)
    assert flat["id"] == "1005006000000001"
    assert flat["rating"] == 4.8
    assert flat["reviews"] == 320
    assert flat["orders"] == 540
    assert flat["price"] == Decimal("17.99")
    assert flat["sku_id"] == "12000012345001"
    assert len(flat["images"]) == 3 and all(u.startswith("https://") for u in flat["images"])


def test_gates_reject_expected() -> None:
    d = _details()
    f = ali_api.flatten_detail
    assert ali_api.gate_reason(f(d[0])) is None                  # eligible
    assert ali_api.gate_reason(f(d[1])) is None                  # eligible (rating 4.6, 90 reviews)
    assert ali_api.gate_reason(f(d[2])) == "excluded brand"      # Apple iPhone
    assert ali_api.gate_reason(f(d[3])) == "reviews < 25"        # only 5 reviews
    assert ali_api.gate_reason(f(d[4])) == "restricted category" # supplement
    assert ali_api.gate_reason(f(d[5])) == f"price < {ali_api.MIN_PRICE_USD}"  # $3.00


def test_source_is_schema_valid() -> None:
    flat = ali_api.flatten_detail(_details()[0])
    source = ali_api.product_to_source(flat, "Smartphone Accessories", "20260722T090000", "2026-07-22")
    normalized = normalize_source(source)  # raises on any schema violation
    assert normalized["product_id"] == "1005006000000001"
    assert normalized["product_id"] in normalized["aliexpress_url"]
    assert len(normalized["listing_title"]) <= 80
    assert normalized["selected_variants"][0]["sku"].startswith("ALI-1005006000000001-")
    assert normalized["aspects"]["Brand"] == ["Unbranded"]
    assert normalized["aspects"]["MPN"] == ["N/A"]
    assert len(normalized["source_images"]) == 3


def test_component_titles_are_rejected() -> None:
    # The "12pcs watercooling fittings" class of product must never be listed.
    for title in [
        "8pcs 12pcs for ID10mm OD16mm Soft Pipes Fitting Connector",
        "M3 Screw Kit Stainless Steel Bolts Nuts Assortment",
        "WiFi Antenna FPC Connector For Samsung S22 Flex Cable",
        "Replacement Keycap Key Cap Scissor Clip For Lenovo Keyboard",
        "Screen INCELL LCD Replacement For iPhone 14 Pro With Tools",
    ]:
        assert ali_api.is_component(title), title
    for title in ["RC Drone 4K Camera Foldable Quadcopter", "LED Strip Light Kit RGB"]:
        assert not ali_api.is_component(title), title


def test_string_list_reads_feed_image_key() -> None:
    # The DS feed nests gallery images under productSmallImageUrl — all must be captured.
    card = {
        "product_id": "1005006000000099",
        "product_title": "USB C Hub Adapter Multiport",
        "target_sale_price": "18.00",
        "evaluate_rate": "96.0%",
        "lastest_volume": "500",
        "product_main_image_url": "https://x/main.jpg",
        "product_small_image_urls": {"productSmallImageUrl": ["https://x/a.jpg", "https://x/b.jpg", "https://x/c.jpg"]},
    }
    flat = ali_api.flatten_card(card)
    assert flat["images"] == ["https://x/main.jpg", "https://x/a.jpg", "https://x/b.jpg", "https://x/c.jpg"]


def test_source_products_finds_two() -> None:
    os.environ["ALI_API_FIXTURE"] = str(FIXTURE)
    sources, _ = ali_api.source_products(
        "Smartphone Accessories", "20260722T090000", "2026-07-22", history=[], needed=2
    )
    assert {s["product_id"] for s in sources} == {"1005006000000001", "1005006000000002"}


def test_history_dedup_skips_known_product() -> None:
    os.environ["ALI_API_FIXTURE"] = str(FIXTURE)
    history = [{"aliexpress_url": "https://www.aliexpress.us/item/1005006000000001.html",
                "ebay_listing_status": "listed"}]
    sources, _ = ali_api.source_products(
        "Smartphone Accessories", "20260722T090000", "2026-07-22", history=history, needed=2
    )
    ids = {s["product_id"] for s in sources}
    assert "1005006000000001" not in ids
    assert ids == {"1005006000000002"}


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
