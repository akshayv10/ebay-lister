#!/usr/bin/env python3
"""Fill each pending draft's gallery with every image from its AliExpress page.

RUN THIS ON YOUR MAC, not in GitHub Actions. AliExpress bot-challenges datacenter IPs,
so the runner gets nothing back; your own connection works, which is why you have been
opening each product page by hand. This does that trip for you and writes the URLs
straight into the Drafts tab.

Without ALIEXPRESS_ACCESS_TOKEN the DS API returns no gallery, no per-SKU images and no
description HTML, so the sheet only ever gets the few feed thumbnails. This closes that
gap without a token. (Minting one is still worth it — it also restores multi-variant
listings and per-SKU shipping. See: python3 mint_ali_token.py --check)

Run:
    python3 collect_images.py                 # fill every pending draft
    python3 collect_images.py --print-only     # just print what it found, write nothing
    python3 collect_images.py --url <product>  # inspect one product, no sheet needed
    python3 collect_images.py --browser        # render with headless Chromium if blocked

Environment:
    GOOGLE_SERVICE_ACCOUNT_JSON   needed to read/write the Drafts tab (not for
                                  --print-only with --url)
    SHEETS_SPREADSHEET_ID, SHEETS_DRAFT_TAB_NAME   as the workflows set them
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import ali_api
import draft_sheet

# eBay's gallery limits, mirroring listing_job.normalize_source.
MAX_SINGLE_VARIANT_IMAGES = 24
MAX_GROUP_IMAGES = 12


def image_cap(variant_count: int) -> int:
    return MAX_GROUP_IMAGES if variant_count > 1 else MAX_SINGLE_VARIANT_IMAGES


def fetch_with_browser(url: str, timeout: int = 45) -> str:
    """Render the page in headless Chromium. Returns '' if Playwright isn't installed.

    Only needed when the plain fetch is challenged. Install with:
        pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 - optional dependency
    except ImportError:
        print("  ! --browser needs Playwright: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return ""
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(user_agent=ali_api.BROWSER_HEADERS["User-Agent"])
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            body = page.content()
            browser.close()
            return body
    except Exception as exc:  # noqa: BLE001 - fall back to whatever the plain fetch got
        print(f"  ! browser render failed: {exc}", file=sys.stderr)
        return ""


def collect(product_url: str, *, use_browser: bool = False,
            include_description: bool = True) -> dict[str, list[str]]:
    """Every image for one product, classified. Empty lists if the page can't be read."""
    body = ali_api.fetch_page(product_url)
    if (not body or not ali_api.page_image_sets(body)["gallery"]) and use_browser:
        body = fetch_with_browser(product_url) or body
    if not body:
        return {"gallery": [], "variant": [], "description": []}

    sets = ali_api.page_image_sets(body)
    if include_description:
        # The description photos live in a separate static CDN document, which is
        # generally served without bot protection even when the product page is not.
        link = ali_api.description_url(body)
        if link:
            document = ali_api.fetch_page(link)
            if document:
                found = ali_api.page_image_sets(document)
                existing = set(sets["gallery"]) | set(sets["variant"]) | set(sets["description"])
                for url in found["gallery"] + found["description"]:
                    if url not in existing:
                        existing.add(url)
                        sets["description"].append(url)
    else:
        sets["description"] = []
    return sets


def merge(current: list[str], sets: dict[str, list[str]], cap: int) -> tuple[list[str], list[str]]:
    """Return (gallery, overflow).

    Order is deliberate: whatever is already there keeps its place — so the existing
    primary photo stays primary — then gallery, then variant, then description. eBay
    uses image 1 as the listing thumbnail, and openai_copy only reads the first 8
    images, so description shots must never lead.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for url in list(current) + sets["gallery"] + sets["variant"] + sets["description"]:
        canonical = ali_api._canonical_image(url)
        if canonical and canonical not in seen:
            seen.add(canonical)
            ordered.append(canonical)
    return ordered[:cap], ordered[cap:]


def _cell(values: list[Any], name: str) -> str:
    index = draft_sheet.COL[name]
    return str(values[index]).strip() if index < len(values) else ""


def cap_for(values: list[Any], draft_id: str = "") -> int:
    """The image cap for a row, erring towards the stricter multi-variant limit.

    A cleared Variants cell does not mean "one variant": clearing an editable cell falls
    back to the stored draft at publish time, so the original variations come back and
    normalize_source then rejects more than 12 group images. When the cell is blank,
    consult the stored draft, and assume multi-variant if it isn't available — writing 12
    images costs nothing, whereas writing 24 makes the publish fail.
    """
    lines = [line for line in _cell(values, "Variants").splitlines() if line.strip()]
    if lines:
        return image_cap(len(lines))
    try:
        import draft_store  # noqa: PLC0415 - only needed for this fallback

        stored = draft_store.load(draft_id).get("source", {}).get("selected_variants") or []
        if stored:
            return image_cap(len(stored))
    except Exception:  # noqa: BLE001 - draft not checked out locally, or unreadable
        pass
    return MAX_GROUP_IMAGES


def added_images(current: list[str], gallery: list[str]) -> list[str]:
    """What the merge would actually change, compared by content not length.

    Length alone lies: if the current cell holds two size variants of one photo, merge
    canonicalizes them to one and can add a new image while the count stays put — which
    would look like "nothing to do" and silently drop the new photo.
    """
    canonical_current = [ali_api._canonical_image(url) for url in current]
    return [url for url in gallery if url not in canonical_current]


def merged_warning(existing: str, message: str) -> str:
    """Append to the Warnings cell instead of replacing it.

    draft_row writes real validation warnings there — missing required item specifics,
    image-import failures, skipped enrichment. Overwriting them would hide exactly what
    the reviewer needs before approving.
    """
    parts = [part.strip() for part in str(existing or "").split("|") if part.strip()]
    if message and message not in parts:
        parts.append(message)
    return " | ".join(parts)


def run(args: argparse.Namespace) -> int:
    if args.url:
        sets = collect(args.url, use_browser=args.browser,
                       include_description=not args.no_description_images)
        total = sum(len(v) for v in sets.values())
        print(json.dumps(sets, indent=2))
        print(f"\n{total} image(s): {len(sets['gallery'])} gallery, "
              f"{len(sets['variant'])} variant, {len(sets['description'])} description")
        if not total:
            print("\nNothing found. The page was probably challenged — retry with --browser.")
            return 1
        return 0

    if draft_sheet.disabled():
        print("DRAFT_SHEET_DISABLED is set; nothing to do.", file=sys.stderr)
        return 1

    sheet = draft_sheet.client()
    rows = draft_sheet.read_all_rows(client_factory=lambda: sheet)
    pending = [
        row for row in rows
        if str(row["values"][draft_sheet.COL["Status"]] if draft_sheet.COL["Status"] < len(row["values"]) else "")
        .strip().casefold() not in {"live", "rejected"}
    ]
    if args.draft_id:
        pending = [row for row in pending if row["draft_id"] == args.draft_id]
    if not pending:
        print("No pending drafts to fill.")
        return 0

    print(f"{len(pending)} pending draft(s).\n")
    updated = 0
    for row in pending:
        values = row["values"]
        url = str(values[draft_sheet.COL["AliExpress URL"]]).strip() if draft_sheet.COL["AliExpress URL"] < len(values) else ""
        title = str(values[draft_sheet.COL["Title"]])[:52] if draft_sheet.COL["Title"] < len(values) else ""
        print(f"- {row['draft_id']}  {title}")
        if not url:
            print("    ! no AliExpress URL in the row; skipped")
            continue

        sets = collect(url, use_browser=args.browser,
                       include_description=not args.no_description_images)
        found = sum(len(v) for v in sets.values())
        if not found:
            # Leave the row alone rather than clearing a gallery we failed to replace.
            print("    ! page returned no images (challenged?); row left untouched")
            continue

        current = draft_sheet.parse_images(_cell(values, "Images"))
        cap = cap_for(values, row["draft_id"])
        gallery, overflow = merge(current, sets, cap)
        added = added_images(current, gallery)
        changed = bool(added) or gallery != [ali_api._canonical_image(url) for url in current]
        print(f"    found {found} ({len(sets['gallery'])} gallery, {len(sets['variant'])} variant, "
              f"{len(sets['description'])} description) → gallery {len(current)} → {len(gallery)} (cap {cap})")

        if args.print_only:
            for image in gallery:
                print(f"      {image}")
            continue
        if not changed and not overflow:
            print("    nothing new; row left untouched")
            continue

        warning = ""
        if sets["description"]:
            # Description images are often banners, size charts or watermarked collages,
            # and eBay can pull a listing over them. Flag the count so trimming is quick.
            warning = (f"{len(sets['description'])} image(s) came from the description "
                       "section — check them before publishing")
        warning = merged_warning(_cell(values, "Warnings"), warning)
        draft_sheet.update_cells(sheet, row["row"], {
            "Images": draft_sheet.encode_images(gallery),
            "Spare Images": draft_sheet.encode_images(overflow),
            **({"Warnings": warning} if warning else {}),
        })
        updated += 1
        print(f"    wrote {len(gallery)} image(s), {len(overflow)} spare")

    print(f"\n{'Would update' if args.print_only else 'Updated'} {updated} row(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="Inspect one product URL and print what it finds.")
    parser.add_argument("--draft-id", default="", help="Only fill this draft.")
    parser.add_argument("--print-only", action="store_true",
                        help="Show what would be written without touching the sheet.")
    parser.add_argument("--browser", action="store_true",
                        help="Render with headless Chromium when the plain fetch is challenged.")
    parser.add_argument("--no-description-images", action="store_true",
                        help="Skip the description section (banners, size charts).")
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 - a readable message beats a traceback here
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
