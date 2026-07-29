#!/usr/bin/env python3
"""The Drafts tab: a read-only status board for prepared, unpublished listings.

One row per draft, showing which batch it belongs to, what it will cost and list at, and
— once published — its live eBay link or the reason it failed. Every cell is written by
the pipeline and overwritten on each sync.

Nothing here is read back. Publishing selects drafts from the records under state/drafts/,
so editing a cell changes nothing and a Sheets outage cannot stop a publish. Adjust
listings on eBay after they go live.

There is deliberately no eBay-side draft: eBay has no public API that creates a Seller
Hub draft (unpublished Inventory offers are invisible in Seller Hub, Trading AddItem
publishes immediately, and Sell Listing API createItemDraft is a limited release).

Environment:
    SHEETS_SPREADSHEET_ID   same workbook as the Auto Lister tab
    SHEETS_DRAFT_TAB_NAME   tab name (default "Drafts")
    GOOGLE_SERVICE_ACCOUNT_JSON
    DRAFT_SHEET_DISABLED=1  skip all Sheets calls (offline testing)
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from sheet_sync import SheetsClient, SheetSyncError, column_letter

DRAFT_TAB_NAME = os.environ.get("SHEETS_DRAFT_TAB_NAME", "Drafts")

HEADERS = [
    "Draft ID",          # 0  key
    "Batch",             # 1  run stamp — the publish button targets the newest one
    "Status",            # 2
    "Created",           # 3
    "Niche",             # 4
    "AliExpress URL",    # 5
    "Thumbnail",         # 6
    "Title",             # 7
    "Description",       # 8
    "Category ID",       # 9
    "Item Specifics",    # 10
    "Images",            # 11
    "Spare Images",      # 12 read-only suggestions
    "Variants",          # 13
    "Delivered Cost USD",  # 14
    "Suggested Price USD",  # 15
    "Price Override USD",  # 16
    "Warnings",          # 17
    "eBay Listing ID",   # 18
    "eBay URL",          # 19
    "Publish Error",     # 20
    "Last Updated",      # 21
]

COL = {name: index for index, name in enumerate(HEADERS)}

CURRENCY_COLUMNS = [(14, 17)]
PERCENT_COLUMNS: list[int] = []
COLUMN_WIDTHS = [
    (0, 1, 210), (1, 2, 90), (2, 3, 110), (3, 4, 150), (4, 5, 130), (5, 6, 240),
    (6, 7, 130), (7, 8, 320), (8, 9, 420), (9, 10, 110), (10, 11, 320),
    (11, 13, 320), (13, 14, 320), (14, 17, 130), (17, 18, 300), (18, 20, 200),
    (20, 21, 300), (21, 22, 170),
]

def client(**kwargs: Any) -> SheetsClient:
    return SheetsClient(
        sheet_name=DRAFT_TAB_NAME,
        headers=HEADERS,
        currency_columns=CURRENCY_COLUMNS,
        percent_columns=PERCENT_COLUMNS,
        column_widths=COLUMN_WIDTHS,
        **kwargs,
    )


def disabled() -> bool:
    return os.environ.get("DRAFT_SHEET_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- encoding
# Each multi-value field has a plain-text form a person can edit in one cell without
# knowing any JSON. encode_* / parse_* are exact inverses; test_drafts.py round-trips them.

def encode_aspects(aspects: dict[str, Any]) -> str:
    lines = []
    for name, values in (aspects or {}).items():
        items = values if isinstance(values, list) else [values]
        joined = ", ".join(str(item).strip() for item in items if str(item).strip())
        if str(name).strip() and joined:
            lines.append(f"{str(name).strip()}: {joined}")
    return "\n".join(lines)


def parse_aspects(text: str) -> dict[str, list[str]]:
    aspects: dict[str, list[str]] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        name, _, values = line.partition(":")
        cleaned = [item.strip() for item in values.split(",") if item.strip()]
        if name.strip() and cleaned:
            aspects[name.strip()] = cleaned
    return aspects


def encode_images(urls: list[str]) -> str:
    return "\n".join(str(url).strip() for url in (urls or []) if str(url).strip())


def parse_images(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    # Accept newline-, comma-, or whitespace-separated URLs so a paste of any shape works.
    for chunk in str(text or "").replace(",", "\n").split():
        url = chunk.strip().strip("<>\"'")
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def encode_variants(variants: list[dict[str, Any]]) -> str:
    """``id | axis=value, axis=value | visible_price | delivered_total`` per line."""
    lines = []
    for variant in variants or []:
        options = ", ".join(
            f"{str(name).strip()}={str(value).strip()}"
            for name, value in (variant.get("options") or {}).items()
        )
        lines.append(
            f"{variant.get('id', '')} | {options} | "
            f"{variant.get('visible_item_price', '')} | {variant.get('delivered_total', '')}"
        )
    return "\n".join(lines)


def parse_variants(text: str, template: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Parse the variants cell back into source.json variant records.

    Fields the cell does not carry (per-variation image, quantity) are preserved from the
    matching stored variant so editing a price never silently drops a variation photo.
    """
    by_id = {str(item.get("id", "")): item for item in (template or [])}
    variants: list[dict[str, Any]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 4 or not parts[0]:
            continue
        options: dict[str, str] = {}
        for pair in parts[1].split(","):
            name, _, value = pair.partition("=")
            if name.strip() and value.strip():
                options[name.strip()] = value.strip()
        stored = by_id.get(parts[0], {})
        record: dict[str, Any] = {
            "id": parts[0],
            "options": options,
            "visible_item_price": parts[2],
            "delivered_total": parts[3],
            "quantity": 1,
        }
        if stored.get("image"):
            record["image"] = stored["image"]
        variants.append(record)
    return variants


# ------------------------------------------------------------------------------- rows

def draft_row(draft: dict[str, Any]) -> list[Any]:
    """Flatten a stored draft into its sheet row. Purely informational."""
    source = draft.get("source", {}) or {}
    variants = source.get("selected_variants") or []
    first = variants[0] if variants else {}
    images = source.get("source_images") or []
    thumbnail = f'=IMAGE("{images[0]}")' if images else ""
    warnings = list((draft.get("validation") or {}).get("warnings") or []) + list(draft.get("notes") or [])
    override = first.get("price_override", "")
    return [
        draft.get("draft_id", ""),
        draft.get("run_stamp", ""),
        draft.get("status", ""),
        draft.get("created_at", ""),
        source.get("assigned_niche", ""),
        source.get("aliexpress_url", ""),
        thumbnail,
        source.get("listing_title", ""),
        source.get("listing_description", ""),
        (draft.get("validation") or {}).get("category_id", ""),
        encode_aspects(source.get("aspects") or {}),
        encode_images(images),
        encode_images(draft.get("spare_images") or []),
        encode_variants(variants),
        first.get("delivered_total", ""),
        first.get("expected_ebay_price", ""),
        override,
        " | ".join(str(item) for item in warnings),
        draft.get("listing_id", ""),
        draft.get("ebay_url", ""),
        draft.get("publish_error", ""),
        draft.get("updated_at", ""),
    ]


# ----------------------------------------------------------------------------- syncing

def sync_drafts(drafts: list[dict[str, Any]], *, client_factory=client) -> dict[str, Any]:
    """Upsert one row per draft. Never raises — a Sheets outage must not lose a draft,
    which is already safely on disk under state/drafts/.
    """
    if disabled():
        return {"status": "skipped", "written": 0, "error": "DRAFT_SHEET_DISABLED"}
    if not drafts:
        return {"status": "synced", "written": 0, "error": ""}
    try:
        sheet = client_factory()
        written = sheet.upsert_rows(
            [(str(draft["draft_id"]), draft_row(draft)) for draft in drafts],
            # USER_ENTERED so the Thumbnail =IMAGE() formula renders as a picture.
            value_input="USER_ENTERED",
        )
    except Exception as exc:  # noqa: BLE001 - the draft file on disk is the source of truth
        return {"status": "error", "written": 0, "error": str(exc)}
    return {"status": "synced", "written": written, "error": ""}


def update_cells(sheet: SheetsClient, row: int, values: dict[str, Any]) -> int:
    """Write specific named cells of one row, leaving every other cell alone.

    Used by collect_images.py, which only owns Images / Spare Images / Warnings. Writing
    the whole row there would revert any title, price or variant edit made in between.
    """
    data = []
    for name, value in values.items():
        letter = column_letter(COL[name])
        cell = f"'{sheet.sheet_name}'!{letter}{row}"
        data.append({"range": cell, "majorDimension": "ROWS", "values": [[value]]})
    if not data:
        return 0
    sheet.request("POST", "/values:batchUpdate", {"valueInputOption": "USER_ENTERED", "data": data})
    return len(data)


def read_all_rows(*, client_factory=client) -> list[dict[str, Any]]:
    """Every draft row as {draft_id, row, values} — approved or not."""
    if disabled():
        return []
    sheet = client_factory()
    sheet.ensure_sheet()
    return [
        {"draft_id": str(values[0]).strip(), "row": row_number, "values": values}
        for row_number, values in sheet.read_rows()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="Rebuild the Drafts tab from state/drafts/")
    sub.add_parser("pending", help="Print the drafts still waiting to be published")
    args = parser.parse_args()
    import draft_store

    try:
        if args.command == "sync":
            print(json.dumps(sync_drafts(draft_store.load_all()), indent=2))
        else:
            print(json.dumps([
                {"draft_id": item["draft_id"], "batch": item.get("run_stamp", ""),
                 "status": item.get("status", ""), "parked": draft_store.is_parked(item)}
                for item in draft_store.pending()
            ], indent=2))
        return 0
    except (SheetSyncError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
