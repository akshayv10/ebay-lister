#!/usr/bin/env python3
"""Offline tests for the AliExpress sourcing mapping and gates. Never hits the network."""

from __future__ import annotations

import os
from pathlib import Path

import ali_api
from listing_job import normalize_source

FIXTURE = Path(__file__).with_name("fixtures") / "ali_sample.json"


def _fixture_products() -> list[dict]:
    os.environ["ALI_API_FIXTURE"] = str(FIXTURE)
    return ali_api.search_products("anything")


def test_field_extraction() -> None:
    products = _fixture_products()
    stand = products[0]
    assert ali_api.extract_product_id(stand) == "1005006000000001"
    assert ali_api.extract_price_usd(stand) == __import__("decimal").Decimal("17.99")
    assert ali_api.extract_evaluate_rate(stand) == 95.2
    assert ali_api.extract_orders(stand) == 540
    images = ali_api.extract_images(stand)
    assert len(images) == 3 and all(u.startswith("https://") for u in images)


def test_gates_reject_expected() -> None:
    products = _fixture_products()
    assert ali_api.gate_reason(products[0]) is None            # eligible
    assert ali_api.gate_reason(products[1]) is None            # eligible
    assert ali_api.gate_reason(products[2]) == "excluded brand"  # Apple iPhone
    assert ali_api.gate_reason(products[3]) is not None        # price too low
    assert ali_api.gate_reason(products[4]) == "restricted category"  # supplement


def test_source_is_schema_valid() -> None:
    products = _fixture_products()
    source = ali_api.product_to_source(products[0], "Smartphone Accessories", "20260722T090000", "2026-07-22")
    # normalize_source raises on any schema violation; a clean return proves compatibility.
    normalized = normalize_source(source)
    assert normalized["product_id"] == "1005006000000001"
    assert normalized["product_id"] in normalized["aliexpress_url"]
    assert len(normalized["listing_title"]) <= 80
    assert normalized["selected_variants"][0]["sku"].startswith("ALI-1005006000000001-")
    assert normalized["aspects"]["Brand"] == ["Unbranded"]


def test_source_products_finds_two() -> None:
    os.environ["ALI_API_FIXTURE"] = str(FIXTURE)
    sources, notes = ali_api.source_products(
        "Smartphone Accessories", "20260722T090000", "2026-07-22", history=[], needed=2
    )
    assert len(sources) == 2
    ids = {s["product_id"] for s in sources}
    assert ids == {"1005006000000001", "1005006000000002"}


def test_history_dedup_skips_known_product() -> None:
    os.environ["ALI_API_FIXTURE"] = str(FIXTURE)
    history = [{"aliexpress_url": "https://www.aliexpress.us/item/1005006000000001.html",
                "ebay_listing_status": "listed"}]
    sources, _ = ali_api.source_products(
        "Smartphone Accessories", "20260722T090000", "2026-07-22", history=history, needed=2
    )
    # Only one other eligible product exists in the fixture, so dedup leaves one.
    ids = {s["product_id"] for s in sources}
    assert "1005006000000001" not in ids
    assert ids == {"1005006000000002"}


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
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
