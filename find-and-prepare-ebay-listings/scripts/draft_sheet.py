#!/usr/bin/env python3
"""The Drafts tab: the human review-and-edit surface for unpublished listings.

One row per draft. The pipeline writes the row; you edit the editable columns and tick
``Publish?`` = YES on the rows you approve. ``publish_drafts.py`` then reads the ticked
rows, layers your edits onto the stored draft, and lists them.

There is deliberately no eBay-side draft: eBay has no public API that creates a Seller
Hub draft (unpublished Inventory offers are invisible in Seller Hub, Trading AddItem
publishes immediately, and Sell Listing API createItemDraft is a limited release), so
this sheet *is* the draft.

Editable columns: Title, Description, Category ID, Item Specifics, Images,
Price Override USD, Variants, Publish?. Everything else is pipeline-owned and is
overwritten on every sync.

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

from sheet_sync import SheetsClient, SheetSyncError

DRAFT_TAB_NAME = os.environ.get("SHEETS_DRAFT_TAB_NAME", "Drafts")

HEADERS = [
    "Draft ID",          # 0  key, pipeline-owned
    "Publish?",          # 1  EDITABLE — tick YES to approve
    "Status",            # 2
    "Created",           # 3
    "Niche",             # 4
    "AliExpress URL",    # 5
    "Thumbnail",         # 6
    "Title",             # 7  EDITABLE
    "Description",       # 8  EDITABLE
    "Category ID",       # 9  EDITABLE
    "Item Specifics",    # 10 EDITABLE
    "Images",            # 11 EDITABLE
    "Spare Images",      # 12 read-only suggestions
    "Variants",          # 13 EDITABLE
    "Delivered Cost USD",  # 14
    "Suggested Price USD",  # 15
    "Price Override USD",  # 16 EDITABLE
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

# Columns a reviewer owns. Everything else is refreshed from the stored draft on sync.
EDITABLE_COLUMNS = (
    "Publish?", "Title", "Description", "Category ID", "Item Specifics",
    "Images", "Variants", "Price Override USD",
)

_TRUE_VALUES = {"yes", "y", "true", "1", "x", "✓", "✔", "approved", "publish"}


def is_approved(value: Any) -> bool:
    """True for the many ways a person might tick a cell (YES, TRUE, a checkbox, ✓)."""
    return str(value or "").strip().casefold() in _TRUE_VALUES


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

def draft_row(draft: dict[str, Any], publish_cell: str | None = None) -> list[Any]:
    """Flatten a stored draft into its sheet row.

    ``publish_cell`` carries the reviewer's current tick through a refresh. A draft that
    went live always comes back as "NO": the approval has been spent, and leaving a
    standing YES next to a live listing is what would let it be published twice if the
    draft record ever reverted.
    """
    source = draft.get("source", {}) or {}
    variants = source.get("selected_variants") or []
    first = variants[0] if variants else {}
    images = source.get("source_images") or []
    thumbnail = f'=IMAGE("{images[0]}")' if images else ""
    warnings = list((draft.get("validation") or {}).get("warnings") or []) + list(draft.get("notes") or [])
    override = first.get("price_override", "")
    approved = "NO" if draft.get("status") == "live" else str(publish_cell or "NO")
    return [
        draft.get("draft_id", ""),
        approved,
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


def row_edits(values: list[Any], draft: dict[str, Any]) -> dict[str, Any]:
    """Extract the reviewer's edits from a sheet row.

    Only non-empty editable cells are returned, so clearing a cell falls back to the
    stored draft rather than wiping a required field.
    """
    def cell(name: str) -> str:
        index = COL[name]
        return str(values[index]).strip() if index < len(values) else ""

    source = draft.get("source", {}) or {}
    edits: dict[str, Any] = {}
    if cell("Title"):
        edits["listing_title"] = cell("Title")
    if cell("Description"):
        edits["listing_description"] = cell("Description")
    if cell("Category ID"):
        edits["category_id"] = cell("Category ID")
    if cell("Item Specifics"):
        parsed = parse_aspects(cell("Item Specifics"))
        if parsed:
            edits["aspects"] = parsed
    if cell("Images"):
        parsed_images = parse_images(cell("Images"))
        if parsed_images:
            edits["source_images"] = parsed_images
    if cell("Variants"):
        parsed_variants = parse_variants(cell("Variants"), source.get("selected_variants") or [])
        if parsed_variants:
            edits["selected_variants"] = parsed_variants
    if cell("Price Override USD"):
        edits["price_override"] = cell("Price Override USD")
    return edits


def apply_edits(draft: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    """Layer sheet edits onto a copy of the draft's source, returning the merged source."""
    source = json.loads(json.dumps(draft.get("source", {}) or {}))
    for key in ("listing_title", "listing_description", "aspects", "source_images"):
        if key in edits:
            source[key] = edits[key]
    if "selected_variants" in edits:
        source["selected_variants"] = edits["selected_variants"]
    # A hand-set price applies to every variation; normalize_source rejects one at or
    # below delivered cost.
    if edits.get("price_override"):
        for variant in source.get("selected_variants") or []:
            variant["price_override"] = edits["price_override"]
    # The category is re-resolved from category_query at prepare time, so an explicit
    # category edit has to steer that query.
    if edits.get("category_id"):
        source["category_id_override"] = edits["category_id"]
    if edits.get("aspects") and source.get("verified_brand"):
        # normalize_source requires Brand to match verified_brand; keep them consistent
        # when a reviewer rewrites the specifics without touching Brand.
        source["aspects"].setdefault("Brand", [source["verified_brand"]])
    return source


# ----------------------------------------------------------------------------- syncing

def sync_drafts(
    drafts: list[dict[str, Any]],
    *,
    client_factory=client,
    publish_cells: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Upsert one row per draft. Never raises — a Sheets outage must not lose a draft,
    which is already safely on disk under state/drafts/.

    ``publish_cells`` maps draft ID to the reviewer's current Publish? value, so a
    status refresh after a failed publish does not silently un-approve the row.
    """
    if disabled():
        return {"status": "skipped", "written": 0, "error": "DRAFT_SHEET_DISABLED"}
    if not drafts:
        return {"status": "synced", "written": 0, "error": ""}
    ticks = publish_cells or {}
    try:
        sheet = client_factory()
        written = sheet.upsert_rows(
            [
                (str(draft["draft_id"]), draft_row(draft, ticks.get(str(draft["draft_id"]))))
                for draft in drafts
            ],
            # USER_ENTERED so the Thumbnail =IMAGE() formula renders as a picture.
            value_input="USER_ENTERED",
        )
    except Exception as exc:  # noqa: BLE001 - the draft file on disk is the source of truth
        return {"status": "error", "written": 0, "error": str(exc)}
    return {"status": "synced", "written": written, "error": ""}


def read_approved(*, client_factory=client) -> list[dict[str, Any]]:
    """Rows the reviewer ticked: [{draft_id, row, values}]. Raises on a Sheets failure —
    publishing must not silently list nothing (or worse, the wrong thing)."""
    if disabled():
        return []
    sheet = client_factory()
    sheet.ensure_sheet()
    approved: list[dict[str, Any]] = []
    for row_number, values in sheet.read_rows():
        if not is_approved(values[COL["Publish?"]] if COL["Publish?"] < len(values) else ""):
            continue
        approved.append({
            "draft_id": str(values[0]).strip(),
            "row": row_number,
            "values": values,
        })
    return approved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="Upsert every stored draft into the Drafts tab")
    sub.add_parser("approved", help="Print the draft IDs currently ticked Publish? = YES")
    args = parser.parse_args()
    import draft_store

    try:
        if args.command == "sync":
            print(json.dumps(sync_drafts(draft_store.load_all()), indent=2))
        else:
            print(json.dumps([item["draft_id"] for item in read_approved()], indent=2))
        return 0
    except (SheetSyncError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
