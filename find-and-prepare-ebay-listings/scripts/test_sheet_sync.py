#!/usr/bin/env python3
"""Offline tests for Google Sheets row mapping, idempotency, and recovery."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import sheet_sync


def live_product(listing_id: str = "123456789012") -> dict:
    return {
        "local_calendar_date": "2026-07-24",
        "run_id": "20260724T120000-product-1005000000000001",
        "assigned_niche": "Home Improvements & Lighting",
        "product_id": "1005000000000001",
        "listing_title": "Rechargeable Motion Sensor Light",
        "aliexpress_url": "https://www.aliexpress.us/item/1005000000000001.html",
        "listing_id": listing_id,
        "ebay_url": f"https://www.ebay.com/itm/{listing_id}",
        "selected_variants": [{
            "sku": "ALI-1005000000000001-default-test",
            "visible_item_price": "20.00",
            "delivered_total": "22.00",
            "expected_ebay_price": "85.00",
        }],
        "aliexpress_rating": 4.8,
        "aliexpress_reviews": 320,
        "aliexpress_orders": 540,
        "appeal_score": 9,
        "appeal_reason": "Clear visual demonstration and broad utility.",
        "general_promotion": {"bid_percentage": "10.0"},
    }


def test_product_record_maps_full_metrics_and_pricing() -> None:
    record = sheet_sync.product_record(live_product())
    values = sheet_sync.row_values(record)
    assert len(values) == len(sheet_sync.HEADERS) == 26
    assert record["sync_key"] == "123456789012"
    assert record["shipping"] == 2.0
    assert record["source_price"] == 20
    assert record["delivered_cost"] == 22
    assert record["list_price"] == 85
    assert record["estimated_ebay_fee"] > 0
    assert record["estimated_promoted_fee"] > 0
    assert record["estimated_profit"] > 0
    assert record["estimated_margin"] >= 50
    assert record["rating"] == 4.8
    assert record["reviews"] == 320
    assert record["orders"] == 540


def test_optional_metrics_remain_blank() -> None:
    product = live_product()
    for key in ("aliexpress_rating", "aliexpress_reviews", "aliexpress_orders",
                "appeal_score", "appeal_reason"):
        product.pop(key)
    record = sheet_sync.product_record(product)
    assert record["rating"] == ""
    assert record["reviews"] == ""
    assert record["orders"] == ""
    assert record["appeal_score"] == ""
    assert record["appeal_reason"] == ""


def test_history_backfill_maps_live_records_only() -> None:
    with tempfile.TemporaryDirectory() as directory:
        history = Path(directory) / "history.jsonl"
        records = [
            {
                "local_calendar_date": "2026-07-24",
                "assigned_niche": "Test",
                "product_title": "Listed item",
                "aliexpress_url": "https://www.aliexpress.us/item/1005000000000001.html",
                "ebay_listing_status": "listed",
                "ebay_item_number": "111",
                "ebay_url": "https://www.ebay.com/itm/111",
                "selected_variants": [{
                    "sku": "sku-1",
                    "visible_item_price": "20.00",
                    "delivered_total": "22.00",
                    "expected_ebay_price": "85.00",
                }],
            },
            {
                "ebay_listing_status": "not_listed",
                "ebay_item_number": "222",
            },
        ]
        history.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
        mapped = sheet_sync.load_history(history)
        assert len(mapped) == 1
        assert mapped[0]["sync_key"] == "111"
        assert mapped[0]["rating"] == ""


def test_sync_failure_queues_and_next_run_replays_without_duplicates() -> None:
    class FailingClient:
        def upsert(self, records):
            raise sheet_sync.SheetSyncError("temporary outage")

    class RecordingClient:
        seen: list[dict] = []

        def upsert(self, records):
            self.seen = list(records)
            RecordingClient.seen = self.seen
            return len(records)

    with tempfile.TemporaryDirectory() as directory:
        pending = Path(directory) / "pending.jsonl"
        record = sheet_sync.product_record(live_product())
        failed = sheet_sync.sync_records(
            [record], client_factory=FailingClient, pending_path=pending
        )
        assert failed["status"] == "queued"
        assert failed["queued"] == 1
        assert len(sheet_sync.load_pending(pending)) == 1

        duplicate = {**record, "appeal_reason": "newer value"}
        synced = sheet_sync.sync_records(
            [duplicate], client_factory=RecordingClient, pending_path=pending
        )
        assert synced == {"status": "synced", "written": 1, "queued": 0, "error": ""}
        assert len(RecordingClient.seen) == 1
        assert RecordingClient.seen[0]["appeal_reason"] == "newer value"
        assert sheet_sync.load_pending(pending) == []


def test_sheet_client_upserts_existing_key_and_appends_new_key() -> None:
    class FakeSheetsClient(sheet_sync.SheetsClient):
        def __init__(self):
            self.spreadsheet_id = "test"
            self.sheet_name = "Auto Lister"
            self.sheet_id = 42
            self.requests: list[tuple] = []

        def ensure_sheet(self):
            return 42

        def existing_rows(self):
            return {"111": 2}

        def request(self, method, path, payload=None, query=None):
            self.requests.append((method, path, payload, query))
            return {}

    client = FakeSheetsClient()
    first = sheet_sync.product_record(live_product("111"))
    second = sheet_sync.product_record(live_product("222"))
    assert client.upsert([first, second]) == 2
    payload = client.requests[-1][2]
    assert [item["range"] for item in payload["data"]] == [
        "'Auto Lister'!A2:Z2",
        "'Auto Lister'!A3:Z3",
    ]


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
