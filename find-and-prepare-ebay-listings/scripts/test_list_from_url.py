#!/usr/bin/env python3
"""Offline tests for the on-demand single-URL lister. Never hits the network or eBay
(the AliExpress detail fetch is monkeypatched to a fixture; --live is never exercised)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import ali_api
import list_from_url

FIXTURE = Path(__file__).with_name("fixtures") / "ali_sample.json"


def _details() -> list[dict]:
    os.environ["ALI_API_FIXTURE"] = str(FIXTURE)
    return ali_api.discover("anything", 1)


def test_url_parsing_accepts_common_forms() -> None:
    cases = {
        "https://www.aliexpress.us/item/1005006000000001.html": "1005006000000001",
        "https://www.aliexpress.com/item/1005006000000001.html?spm=a2g0o.x": "1005006000000001",
        "aliexpress.us/item/1005006000000001.html": "1005006000000001",
        "https://www.aliexpress.com/i/1005006000000001.html": "1005006000000001",
    }
    for url, expected in cases.items():
        assert ali_api.product_id_from_url(url) == expected, url


def test_url_parsing_rejects_non_aliexpress() -> None:
    for bad in ("https://www.ebay.com/itm/1005006000000001", "https://example.com/item/123.html"):
        try:
            ali_api.product_id_from_url(bad)
        except ali_api.AliError:
            continue
        raise AssertionError(f"expected AliError for {bad}")


def test_enforce_gates_flag() -> None:
    failing = ali_api.flatten_detail(_details()[3])  # reviews < 25
    assert ali_api.gate_reason(failing) == "reviews < 25"
    # Default (daily run) still raises on a gate failure.
    try:
        ali_api.product_to_source(failing, "n", "20260101T000000", "2026-01-01")
        raise AssertionError("enforce_gates=True should have raised")
    except ali_api.AliError:
        pass
    # On-demand (enforce_gates=False) builds the source despite the soft-gate failure.
    source = ali_api.product_to_source(failing, "on-demand", "20260101T000000", "2026-01-01",
                                       enforce_gates=False)
    assert source["product_id"] == failing["id"]


def test_build_source_reports_gate_warning() -> None:
    detail = _details()[3]  # reviews < 25 -> soft warning, still listable
    orig = ali_api.get_product_detail
    ali_api.get_product_detail = lambda pid: detail
    try:
        source, warning = list_from_url.build_source(
            "https://www.aliexpress.us/item/1005006000000004.html", "20260101T000000", "2026-01-01")
    finally:
        ali_api.get_product_detail = orig
    assert warning == "reviews < 25"
    assert source["listing_title"]


def test_dry_run_result_shape() -> None:
    detail = _details()[0]  # fully eligible
    orig = ali_api.get_product_detail
    ali_api.get_product_detail = lambda pid: detail
    with tempfile.TemporaryDirectory() as tmp:
        list_from_url.RUNS_DIR = Path(tmp)
        try:
            result = list_from_url.list_one(
                "https://www.aliexpress.us/item/1005006000000001.html", live=False)
        finally:
            ali_api.get_product_detail = orig
    assert result["status"] == "partial"          # dry run
    assert result["listed_count"] == 0
    assert result["niche"] == "on-demand"
    assert result["products"][0]["product_id"] == "1005006000000001"
    assert result["products"][0]["ebay_url"] == ""  # nothing published
    # A fully-eligible product produces no gate warning.
    assert not any("gate warning" in n.lower() for n in result["notes"])


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
