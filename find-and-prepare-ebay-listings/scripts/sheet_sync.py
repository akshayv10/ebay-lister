#!/usr/bin/env python3
"""Idempotently sync live eBay listings into a Google Sheets tracking tab.

Authentication uses a service-account JSON document in
``GOOGLE_SERVICE_ACCOUNT_JSON``. Failed writes are stored as row records in
``PENDING_SHEET_SYNC_PATH`` and replayed on the next invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ebay_price import quote

SPREADSHEET_ID = os.environ.get(
    "SHEETS_SPREADSHEET_ID", "10GgtsN_cxhHBvbEYa4vUXBUbC-LqeElkzmRiL3TT0Uk"
)
SHEET_NAME = os.environ.get("SHEETS_TAB_NAME", "Auto Lister")
PENDING_PATH = Path(os.environ.get("PENDING_SHEET_SYNC_PATH", "state/pending-sheet-sync.jsonl"))
HISTORY_PATH = Path(os.environ.get("HISTORY_PATH", "state/resale-product-history.jsonl"))
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

HEADERS = [
    "Sync Key",
    "Listing Date",
    "Run ID",
    "Status",
    "Niche",
    "Product Title",
    "AliExpress Product ID",
    "AliExpress URL",
    "eBay Listing ID",
    "eBay URL",
    "SKU",
    "Source Price USD",
    "Shipping / Estimate USD",
    "Delivered Cost USD",
    "eBay List Price USD",
    "Estimated eBay Fee USD",
    "Estimated Promoted Fee USD",
    "Estimated Profit USD",
    "Estimated Margin %",
    "AliExpress Rating",
    "Reviews",
    "Orders",
    "AI Appeal Score / 10",
    "AI Appeal Reason",
    "Promotion Rate %",
    "Last Synced At",
]


class SheetSyncError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _number(value: Any) -> float | int | str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return int(number) if number.is_integer() else number


def _product_id_from_url(url: str) -> str:
    tail = url.split("/item/", 1)[-1].split(".html", 1)[0]
    return tail if tail.isdigit() else ""


def _variant(product: dict[str, Any]) -> dict[str, Any]:
    variants = product.get("selected_variants") or []
    return variants[0] if variants and isinstance(variants[0], dict) else {}


def _pricing(variant: dict[str, Any]) -> dict[str, Any]:
    delivered = _number(variant.get("delivered_total"))
    if delivered == "":
        return {}
    return quote(float(delivered))


def product_record(product: dict[str, Any]) -> dict[str, Any]:
    """Convert one live result product into the durable queue/write shape."""
    listing_id = str(product.get("listing_id") or product.get("ebay_item_number") or "").strip()
    if not listing_id:
        raise SheetSyncError("Live product is missing its eBay listing ID")
    variant = _variant(product)
    pricing = _pricing(variant)
    visible = _number(variant.get("visible_item_price"))
    delivered = _number(variant.get("delivered_total"))
    shipping = ""
    if visible != "" and delivered != "":
        shipping = round(max(0.0, float(delivered) - float(visible)), 2)
    promotion = product.get("general_promotion") or {}
    return {
        "sync_key": listing_id,
        "listing_date": str(product.get("local_calendar_date") or ""),
        "run_id": str(product.get("run_id") or ""),
        "status": "live",
        "niche": str(product.get("assigned_niche") or ""),
        "title": str(product.get("listing_title") or product.get("source_title") or product.get("product_title") or ""),
        "product_id": str(product.get("product_id") or _product_id_from_url(str(product.get("aliexpress_url") or ""))),
        "aliexpress_url": str(product.get("aliexpress_url") or ""),
        "listing_id": listing_id,
        "ebay_url": str(product.get("ebay_url") or ""),
        "sku": str(variant.get("sku") or ""),
        "source_price": visible,
        "shipping": shipping,
        "delivered_cost": delivered,
        "list_price": _number(variant.get("expected_ebay_price")),
        "estimated_ebay_fee": pricing.get("estimated_final_value_fee", ""),
        "estimated_promoted_fee": pricing.get("estimated_promoted_fee", ""),
        "estimated_profit": pricing.get("estimated_profit", ""),
        "estimated_margin": pricing.get("estimated_margin_percent", ""),
        "rating": _number(product.get("aliexpress_rating")),
        "reviews": _number(product.get("aliexpress_reviews")),
        "orders": _number(product.get("aliexpress_orders")),
        "appeal_score": _number(product.get("appeal_score")),
        "appeal_reason": str(product.get("appeal_reason") or ""),
        "promotion_rate": _number(promotion.get("bid_percentage") or pricing.get("promoted_rate_percent")),
        "last_synced_at": _now_iso(),
    }


def history_record(record: dict[str, Any]) -> dict[str, Any]:
    """Convert an existing history entry into the same durable row shape."""
    return product_record({
        **record,
        "listing_id": record.get("ebay_item_number"),
        "listing_title": record.get("product_title"),
        "run_id": record.get("run_id", ""),
        "general_promotion": {"bid_percentage": "10.0"},
    })


def row_values(record: dict[str, Any]) -> list[Any]:
    return [
        record.get("sync_key", ""),
        record.get("listing_date", ""),
        record.get("run_id", ""),
        record.get("status", ""),
        record.get("niche", ""),
        record.get("title", ""),
        record.get("product_id", ""),
        record.get("aliexpress_url", ""),
        record.get("listing_id", ""),
        record.get("ebay_url", ""),
        record.get("sku", ""),
        record.get("source_price", ""),
        record.get("shipping", ""),
        record.get("delivered_cost", ""),
        record.get("list_price", ""),
        record.get("estimated_ebay_fee", ""),
        record.get("estimated_promoted_fee", ""),
        record.get("estimated_profit", ""),
        record.get("estimated_margin", ""),
        record.get("rating", ""),
        record.get("reviews", ""),
        record.get("orders", ""),
        record.get("appeal_score", ""),
        record.get("appeal_reason", ""),
        record.get("promotion_rate", ""),
        record.get("last_synced_at", ""),
    ]


def load_history(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("ebay_listing_status") == "listed" and item.get("ebay_item_number"):
            records.append(history_record(item))
    return records


def load_pending(path: Path = PENDING_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def save_pending(records: list[dict[str, Any]], path: Path = PENDING_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def merge_records(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for record in group:
            key = str(record.get("sync_key") or "").strip()
            if key:
                merged[key] = record
    return list(merged.values())


class _GoogleAuthResponse:
    def __init__(self, status: int, data: bytes, headers: dict[str, str]):
        self.status = status
        self.data = data
        self.headers = headers


class _GoogleAuthRequest:
    """Minimal stdlib transport accepted by google-auth credential refresh."""

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        **_: Any,
    ) -> _GoogleAuthResponse:
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or 30) as response:
                return _GoogleAuthResponse(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as exc:
            return _GoogleAuthResponse(exc.code, exc.read(), dict(exc.headers))


def service_account_token(raw_json: str) -> str:
    if not raw_json.strip():
        raise SheetSyncError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    try:
        info = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise SheetSyncError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise SheetSyncError("google-auth is not installed") from exc
    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    credentials.refresh(_GoogleAuthRequest())
    if not credentials.token:
        raise SheetSyncError("Google service-account token refresh returned no access token")
    return str(credentials.token)


class SheetsClient:
    def __init__(
        self,
        spreadsheet_id: str = SPREADSHEET_ID,
        sheet_name: str = SHEET_NAME,
        token: str | None = None,
    ):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.token = token or service_account_token(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
        self.sheet_id: int | None = None

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{SHEETS_API}/{self.spreadsheet_id}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SheetSyncError(f"Google Sheets API {method} failed ({exc.code}): {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise SheetSyncError(f"Google Sheets API request failed: {exc}") from exc

    def ensure_sheet(self) -> int:
        metadata = self.request("GET", "", query={"fields": "sheets.properties"})
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == self.sheet_name:
                self.sheet_id = int(properties["sheetId"])
                break
        if self.sheet_id is None:
            response = self.request("POST", ":batchUpdate", {
                "requests": [{
                    "addSheet": {
                        "properties": {
                            "title": self.sheet_name,
                            "gridProperties": {
                                "rowCount": 1000,
                                "columnCount": len(HEADERS),
                                "frozenRowCount": 1,
                            },
                        }
                    }
                }]
            })
            self.sheet_id = int(response["replies"][0]["addSheet"]["properties"]["sheetId"])
        self._ensure_headers_and_format()
        return self.sheet_id

    def _ensure_headers_and_format(self) -> None:
        assert self.sheet_id is not None
        encoded = urllib.parse.quote(f"'{self.sheet_name}'!A1:Z1", safe="")
        self.request("PUT", f"/values/{encoded}", {
            "range": f"'{self.sheet_name}'!A1:Z1",
            "majorDimension": "ROWS",
            "values": [HEADERS],
        }, query={"valueInputOption": "RAW"})
        currency_columns = [(11, 18)]
        requests: list[dict[str, Any]] = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": self.sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(HEADERS),
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
                            "textFormat": {"bold": True},
                            "verticalAlignment": "MIDDLE",
                            "wrapStrategy": "WRAP",
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": self.sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "setBasicFilter": {
                    "filter": {
                        "range": {
                            "sheetId": self.sheet_id,
                            "startRowIndex": 0,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(HEADERS),
                        }
                    }
                }
            },
        ]
        for start, end in currency_columns:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": self.sheet_id,
                        "startRowIndex": 1,
                        "startColumnIndex": start,
                        "endColumnIndex": end,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "CURRENCY", "pattern": "$0.00"}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            })
        for column in (18, 24):
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": self.sheet_id,
                        "startRowIndex": 1,
                        "startColumnIndex": column,
                        "endColumnIndex": column + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "NUMBER", "pattern": "0.00\"%\""}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            })
        widths = [
            (0, 4, 120), (4, 5, 210), (5, 6, 320), (6, 11, 150), (11, 23, 120),
            (23, 24, 300), (24, 25, 140), (25, 26, 200),
        ]
        for start, end, pixels in widths:
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": self.sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": start,
                        "endIndex": end,
                    },
                    "properties": {"pixelSize": pixels},
                    "fields": "pixelSize",
                }
            })
        self.request("POST", ":batchUpdate", {"requests": requests})

    def existing_rows(self) -> dict[str, int]:
        encoded = urllib.parse.quote(f"'{self.sheet_name}'!A2:A", safe="")
        response = self.request("GET", f"/values/{encoded}")
        values = response.get("values", [])
        return {
            str(row[0]): index + 2
            for index, row in enumerate(values)
            if row and str(row[0]).strip()
        }

    def upsert(self, records: list[dict[str, Any]]) -> int:
        self.ensure_sheet()
        existing = self.existing_rows()
        updates: list[dict[str, Any]] = []
        next_row = max(existing.values(), default=1) + 1
        for record in records:
            key = str(record["sync_key"])
            row = existing.get(key)
            if row is None:
                row = next_row
                next_row += 1
                existing[key] = row
            updates.append({
                "range": f"'{self.sheet_name}'!A{row}:Z{row}",
                "majorDimension": "ROWS",
                "values": [row_values(record)],
            })
        if updates:
            self.request("POST", "/values:batchUpdate", {
                "valueInputOption": "RAW",
                "data": updates,
            })
        return len(updates)


def sync_records(
    records: list[dict[str, Any]],
    *,
    client_factory: Callable[[], SheetsClient] = SheetsClient,
    pending_path: Path = PENDING_PATH,
) -> dict[str, Any]:
    pending = load_pending(pending_path)
    combined = merge_records(pending, records)
    if not combined:
        save_pending([], pending_path)
        return {"status": "synced", "written": 0, "queued": 0, "error": ""}
    try:
        written = client_factory().upsert(combined)
    except Exception as exc:  # noqa: BLE001 - the durable queue is the recovery boundary
        save_pending(combined, pending_path)
        return {"status": "queued", "written": 0, "queued": len(combined), "error": str(exc)}
    save_pending([], pending_path)
    return {"status": "synced", "written": written, "queued": 0, "error": ""}


def sync_products(
    products: list[dict[str, Any]],
    *,
    client_factory: Callable[[], SheetsClient] = SheetsClient,
    pending_path: Path = PENDING_PATH,
) -> dict[str, Any]:
    records = [product_record(product) for product in products if product.get("listing_id")]
    return sync_records(records, client_factory=client_factory, pending_path=pending_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill-history", action="store_true",
                        help="Upsert every live record from HISTORY_PATH before replaying the pending queue.")
    args = parser.parse_args()
    records = load_history() if args.backfill_history else []
    result = sync_records(records)
    print(json.dumps(result))
    return 0 if result["status"] == "synced" else 1


if __name__ == "__main__":
    raise SystemExit(main())
