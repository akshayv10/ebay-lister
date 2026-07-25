#!/usr/bin/env python3
"""Offline tests for the strict find-resale-products handoff adapter."""

from __future__ import annotations

import base64
import json
import os
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import handoff_batch


FIXTURE = Path(__file__).with_name("fixtures") / "ali_sample.json"


def fixture() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def product(pid: str, title: str, fingerprint: str, price: str) -> dict:
    return {
        "product_id": pid,
        "aliexpress_url": f"https://www.aliexpress.us/item/{pid}.html?tracking=removed",
        "product_title": title,
        "functional_fingerprint": fingerprint,
        "verified_brand": "Unbranded",
        "brand_evidence": "No brand in title, brand field, product images, variant, or packaging.",
        "rating": 4.8,
        "reviews": 320,
        "orders": 540,
        "us_region_confirmed": True,
        "selected_variant": {
            "options": {},
            "display_label": "Default, quantity 1",
            "visible_item_price": price,
            "checkout_total": "Unavailable",
        },
        "material_risk": "No material risk observed.",
    }


def envelope() -> dict:
    value = {
        "schema_version": 1,
        "local_calendar_date": "2026-07-25",
        "assigned_niche": "Smartphone Accessories",
        "products": [
            product(
                "1005006000000001",
                "Universal Aluminum Phone Stand Holder Foldable Desk Adjustable",
                "folding aluminum phone stand",
                "17.99",
            ),
            {
                **product(
                    "1005006000000002",
                    "Magnetic Wireless Car Charger Mount 15W Fast Charging Auto Clamp",
                    "magnetic wireless car charger mount",
                    "22.50",
                ),
                "rating": 4.6,
                "reviews": 90,
                "orders": 620,
            },
        ],
    }
    normalized = handoff_batch.validate_envelope(value)
    value["batch_id"] = normalized["batch_id"]
    return value


def test_deterministic_batch_id_ignores_product_order() -> None:
    first = handoff_batch.validate_envelope(envelope())
    reversed_payload = envelope()
    reversed_payload["products"].reverse()
    reversed_payload.pop("batch_id")
    second = handoff_batch.validate_envelope(reversed_payload)
    assert first["batch_id"] == second["batch_id"]


def test_validation_requires_two_distinct_products_and_evidence() -> None:
    for mutate in (
        lambda value: value["products"].pop(),
        lambda value: value["products"][1].update(
            {"functional_fingerprint": value["products"][0]["functional_fingerprint"]}
        ),
        lambda value: value["products"][0].update({"us_region_confirmed": False}),
        lambda value: value["products"][0].update({"brand_evidence": ""}),
    ):
        value = envelope()
        value.pop("batch_id")
        mutate(value)
        try:
            handoff_batch.validate_envelope(value)
        except handoff_batch.HandoffError:
            continue
        raise AssertionError("Invalid handoff unexpectedly passed")


def test_decode_rejects_oversized_input() -> None:
    try:
        handoff_batch.decode_payload("A" * (handoff_batch.MAX_DISPATCH_INPUT + 1))
    except handoff_batch.HandoffError:
        return
    raise AssertionError("Oversized dispatch input unexpectedly passed")


def test_source_refetches_current_price_and_keeps_exact_product() -> None:
    payload = handoff_batch.validate_envelope(envelope())
    details = fixture()
    with patch("handoff_batch.ali_api.get_product_detail", return_value=details[0]), \
         patch("handoff_batch.ali_api.freight", return_value=Decimal("2.00")):
        source = handoff_batch.source_from_product(payload["products"][0], payload)
    variant = source["selected_variants"][0]
    assert variant["id"] == "sku-12000012345001"
    assert variant["visible_item_price"] == "17.99"
    assert variant["delivered_total"] == "19.99"
    assert source["verified_brand"] == "Unbranded"
    assert source["handoff_evidence"]["browser_visible_item_price"] == "17.99"


def test_structured_variant_must_resolve_uniquely() -> None:
    detail = deepcopy(fixture()[0])
    detail["sku_properties"] = [
        {"sku_property_id": "14", "sku_property_name": "Color"},
        {"sku_property_id": "5", "sku_property_name": "Size"},
    ]
    detail["ae_item_sku_info_dtos"]["ae_item_sku_info_d_t_o"] = [
        {
            "sku_id": "sku-black-large",
            "offer_sale_price": "19.00",
            "sku_attr": "14:1#Black;5:2#Large",
            "sku_image": "https://example.com/black.jpg",
        },
        {
            "sku_id": "sku-white-large",
            "offer_sale_price": "20.00",
            "sku_attr": "14:2#White;5:2#Large",
            "sku_image": "https://example.com/white.jpg",
        },
    ]
    selected = {
        "options": {"Color": "White", "Size": "Large"},
        "display_label": "White / Large",
        "visible_item_price": "20.00",
        "checkout_total": "",
    }
    match = handoff_batch.exact_variant(detail, selected)
    assert match["sku_id"] == "sku-white-large"
    selected["options"] = {"Size": "Large"}
    selected["visible_item_price"] = "19.50"
    try:
        handoff_batch.exact_variant(detail, selected)
    except handoff_batch.HandoffError:
        return
    raise AssertionError("Ambiguous variant unexpectedly resolved")


def test_selected_variant_price_not_cheapest_sku_controls_viability() -> None:
    payload = handoff_batch.validate_envelope(envelope())
    product_value = payload["products"][0]
    product_value["selected_variant"]["options"] = {"Color": "Black"}
    product_value["selected_variant"]["display_label"] = "Black"
    product_value["selected_variant"]["visible_item_price"] = "19.00"
    detail = deepcopy(fixture()[0])
    detail["sku_properties"] = [{"sku_property_id": "14", "sku_property_name": "Color"}]
    detail["ae_item_sku_info_dtos"]["ae_item_sku_info_d_t_o"] = [
        {"sku_id": "cheap-white", "offer_sale_price": "3.00", "sku_attr": "14:1#White"},
        {"sku_id": "selected-black", "offer_sale_price": "19.00", "sku_attr": "14:2#Black"},
    ]
    with patch("handoff_batch.ali_api.get_product_detail", return_value=detail), \
         patch("handoff_batch.ali_api.freight", return_value=None):
        source = handoff_batch.source_from_product(product_value, payload)
    assert source["selected_variants"][0]["id"] == "sku-selected-black"
    assert source["selected_variants"][0]["visible_item_price"] == "19.00"


def test_build_run_blocks_only_the_failed_product(tmp_path: Path | None = None) -> None:
    import tempfile

    payload = handoff_batch.validate_envelope(envelope())
    details = fixture()
    responses = [details[0], details[3]]  # second has too few reviews
    with tempfile.TemporaryDirectory() as directory, \
         patch("handoff_batch.ali_api.get_product_detail", side_effect=responses), \
         patch("handoff_batch.ali_api.freight", return_value=None):
        result = handoff_batch.build_run(payload, Path(directory) / payload["batch_id"])
        assert result["status"] == "source_validated_partial"
        assert result["products"][0]["status"] == "source_validated"
        assert result["products"][1]["status"] == "blocked"


def test_base64_round_trip() -> None:
    raw = json.dumps(envelope(), sort_keys=True).encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    assert handoff_batch.decode_payload(encoded)["batch_id"].startswith("frp-20260725-")


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
