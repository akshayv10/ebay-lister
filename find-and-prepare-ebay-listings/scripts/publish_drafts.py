#!/usr/bin/env python3
"""Publish the drafts you approved in the Drafts sheet tab.

This is the second half of the review-before-publish flow. ``daily_run.py --draft``
sources, enriches, validates and saves drafts; you review them in the Drafts tab, edit
whatever you like, and tick ``Publish?`` = YES. This script then:

  1. reads the ticked rows,
  2. layers your edits onto the stored draft,
  3. re-checks the AliExpress cost so a stale draft cannot be listed at a dead margin,
  4. lists it through the unchanged ``ebay_listing.list_one`` path (prepare, publish,
     10% General promotion), with enrichment disabled so the AI never overwrites you,
  5. records history, syncs the Auto Lister tab, updates the draft row, and emails.

Each draft is independent: one failure never blocks the others, and a draft that fails
stays publishable so you can fix it and re-approve.

Run:
    python publish_drafts.py             # dry run: resolve + validate, publish nothing
    python publish_drafts.py --live      # publish every approved draft
    python publish_drafts.py --live --draft-id <id>   # publish exactly one

Environment: the same eBay / AliExpress / email / Sheets configuration as daily_run.py,
plus DRAFT_MAX_COST_DRIFT_PCT (default 10).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import ali_api
import draft_sheet
import draft_store
import notify
from ebay_common import EbayError, write_json
from listing_job import normalize_source

RUN_TZ = os.environ.get("RUN_TZ", "Asia/Kolkata")
HISTORY_PATH = Path(os.environ.get("HISTORY_PATH", "state/resale-product-history.jsonl"))
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "ebay-listing-runs"))
MAX_COST_DRIFT_PCT = Decimal(os.environ.get("DRAFT_MAX_COST_DRIFT_PCT", "10"))


def _now() -> datetime:
    return datetime.now(ZoneInfo(RUN_TZ))


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return result if result.is_finite() else None


def cost_check(source: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Re-price the draft against AliExpress right now.

    Returns ``(ok, message, refreshed_source)``. A draft can sit in the sheet for days,
    so we refuse to list one whose delivered cost has risen beyond
    DRAFT_MAX_COST_DRIFT_PCT — the computed eBay price would no longer earn its margin.
    A cost that fell is fine: the source is refreshed so the price recomputes downward.

    A lookup failure is *not* fatal — it returns ok=True with a note, because a
    transient AliExpress error must not silently drop an approved listing.
    """
    product_id = str(source.get("product_id", ""))
    variants = source.get("selected_variants") or []
    stored = _decimal((variants[0] if variants else {}).get("delivered_total"))
    if stored is None or stored <= 0:
        return True, "", source

    try:
        flat = ali_api.flatten_detail(ali_api.get_product_detail(product_id))
    except Exception as exc:  # noqa: BLE001 - transient lookup failure is advisory
        return True, f"Could not re-check AliExpress cost ({exc}); listing at the drafted price.", source

    price = flat.get("price")
    if not flat.get("id") or price is None:
        return False, "Product is no longer available on AliExpress.", source

    shipping = ali_api.freight(product_id, flat.get("sku_id", ""), price)
    current = ali_api.delivered_total(price, shipping)
    drift = (current - stored) / stored * Decimal("100")
    if drift > MAX_COST_DRIFT_PCT:
        return False, (
            f"Delivered cost rose {drift:.1f}% since drafting "
            f"(USD {stored:.2f} → USD {current:.2f}), above the "
            f"{MAX_COST_DRIFT_PCT:.0f}% limit. Re-draft or raise the limit."
        ), source

    message = ""
    if abs(drift) >= Decimal("0.5"):
        message = f"Delivered cost moved {drift:+.1f}% (USD {stored:.2f} → USD {current:.2f}); repriced."
    # Refresh every variation that has no hand-set price. A single-variation draft tracks
    # the product price directly; multi-variation prices came from per-SKU lookups, so
    # only the aggregate is refreshed here.
    if len(variants) == 1 and not variants[0].get("price_override"):
        variants[0]["visible_item_price"] = f"{price:.2f}"
        variants[0]["delivered_total"] = f"{current:.2f}"
    return True, message, source


def publish_one(
    draft: dict[str, Any],
    edits: dict[str, Any],
    client: Any,
    config: dict[str, Any],
    run_root: Path,
) -> dict[str, Any]:
    """Publish a single approved draft. Returns a result dict; never raises."""
    from ebay_listing import list_one

    draft_id = str(draft.get("draft_id", ""))
    outcome: dict[str, Any] = {"draft_id": draft_id, "status": "error", "notes": []}

    merged = draft_sheet.apply_edits(draft, edits)
    ok, message, merged = cost_check(merged)
    if message:
        outcome["notes"].append(message)
    if not ok:
        draft_store.mark(draft, draft_store.STATUS_BLOCKED, publish_error=message)
        outcome.update({"status": "blocked", "error": message})
        return outcome

    try:
        # Validate the edited source before touching eBay, so a typo in the sheet is a
        # clear message rather than a half-created listing. Keep the normalized result:
        # it carries the final prices, so the draft record and the listing agree.
        merged = normalize_source(merged)
    except (EbayError, ValueError) as exc:
        draft_store.mark(draft, draft_store.STATUS_BLOCKED, publish_error=str(exc))
        outcome.update({"status": "blocked", "error": f"Edited draft is invalid: {exc}"})
        return outcome

    product_dir = run_root / draft_id / "product-1"
    product_dir.mkdir(parents=True, exist_ok=True)
    source_path = product_dir / "source.json"
    write_json(source_path, merged)

    draft_store.mark(draft, draft_store.STATUS_PUBLISHING, publish_error="")
    try:
        # enrich=False: this source has already been enriched and then hand-edited.
        product = list_one(client, config, source_path, enrich=False)
    except (EbayError, OSError, ValueError) as exc:
        draft_store.mark(draft, draft_store.STATUS_PUBLISH_FAILED, publish_error=str(exc))
        outcome.update({"status": "publish_failed", "error": str(exc)})
        return outcome

    draft_store.mark(
        draft,
        draft_store.STATUS_LIVE,
        listing_id=product.get("listing_id", ""),
        ebay_url=product.get("ebay_url", ""),
        publish_error="",
        source=merged,
    )
    outcome.update({
        "status": "live",
        "product": product,
        "listing_id": product.get("listing_id", ""),
        "ebay_url": product.get("ebay_url", ""),
        "title": merged.get("listing_title", ""),
    })
    return outcome


def run(live: bool, only_draft_id: str = "") -> dict[str, Any]:
    now = _now()
    result: dict[str, Any] = {
        "date": now.date().isoformat(),
        "niche": "approved drafts",
        "status": "error",
        "expected_count": 0,
        "products": [],
        "listed_count": 0,
        "notes": [],
    }

    try:
        approved = draft_sheet.read_approved()
    except Exception as exc:  # noqa: BLE001 - without the sheet we cannot know what to list
        result["error"] = f"Could not read the Drafts tab: {exc}"
        return result

    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in approved:
        draft_id = row["draft_id"]
        if only_draft_id and draft_id != only_draft_id:
            continue
        try:
            draft = draft_store.load(draft_id)
        except EbayError as exc:
            result["notes"].append(f"{draft_id}: {exc}")
            continue
        if not draft_store.is_publishable(draft):
            # Already live (or rejected) — the tick is left over from a previous run.
            result["notes"].append(
                f"{draft_id}: skipped, status is '{draft.get('status')}' (not publishable)"
            )
            continue
        selected.append((draft, draft_sheet.row_edits(row["values"], draft)))

    result["expected_count"] = len(selected)
    if not selected:
        result["status"] = "noop"
        result["error"] = (
            "No approved drafts to publish. Tick Publish? = YES in the Drafts tab first."
        )
        return result

    if not live:
        result["status"] = "partial"
        result["error"] = f"DRY RUN — {len(selected)} approved draft(s) would be published."
        result["products"] = [
            {
                "title": draft.get("source", {}).get("listing_title", ""),
                "aliexpress_url": draft.get("source", {}).get("aliexpress_url", ""),
                "price": (draft.get("source", {}).get("selected_variants") or [{}])[0].get(
                    "visible_item_price", ""),
                "ebay_url": "",
                "reason": "dry run",
            }
            for draft, _ in selected
        ]
        return result

    from ebay_common import EbayClient
    from ebay_listing import history_record, require_setup, write_history_batch

    # Unattended: fill any category-required item specifics the reviewer did not set.
    os.environ.setdefault("EBAY_AUTOFILL_REQUIRED_ASPECTS", "1")
    client = EbayClient()
    config = require_setup(client)
    run_root = RUNS_DIR / f"publish-{now.strftime('%Y%m%dT%H%M%S')}"

    live_products: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for draft, edits in selected:
        outcome = publish_one(draft, edits, client, config, run_root)
        outcomes.append(outcome)
        result["notes"] += outcome.get("notes", [])
        if outcome["status"] == "live":
            live_products.append(outcome["product"])
        else:
            result["notes"].append(f"{outcome['draft_id']}: {outcome.get('error', 'failed')}")

    if live_products:
        try:
            write_history_batch(
                HISTORY_PATH,
                [
                    history_record(p, {"listing_id": p["listing_id"], "ebay_url": p["ebay_url"]})
                    for p in live_products
                ],
            )
        except (OSError, ValueError) as exc:
            result["notes"].append(f"history: {exc}")
        try:
            import sheet_sync

            result["sheet_sync"] = sheet_sync.sync_products(live_products)
        except Exception as exc:  # noqa: BLE001 - listing success must survive tracking failures
            result["sheet_sync"] = {
                "status": "queued", "written": 0, "queued": len(live_products),
                "error": f"Could not prepare Google Sheets sync: {exc}",
            }

    # Refresh every touched draft's row so the sheet shows the live link (or the failure)
    # right next to what was reviewed.
    sheet_update = draft_sheet.sync_drafts([draft for draft, _ in selected])
    if sheet_update.get("error"):
        result["notes"].append(f"Drafts sheet: {sheet_update['error']}")

    result["products"] = [
        {
            "title": outcome.get("title", ""),
            "aliexpress_url": "",
            "price": "",
            "ebay_price": "",
            "ebay_url": outcome.get("ebay_url", ""),
            "listing_id": outcome.get("listing_id", ""),
            "reason": outcome.get("error", ""),
        }
        for outcome in outcomes
    ]
    for outcome, product in zip(outcomes, result["products"]):
        if outcome["status"] == "live":
            source = outcome["product"]
            variant = (source.get("selected_variants") or [{}])[0]
            product["aliexpress_url"] = source.get("aliexpress_url", "")
            product["price"] = variant.get("visible_item_price", "")
            product["ebay_price"] = variant.get("expected_ebay_price", "")

    result["listed_count"] = len(live_products)
    if result["listed_count"] == result["expected_count"]:
        result["status"] = "listed"
    elif live_products:
        result["status"] = "partial"
        result["error"] = (
            f"Published {len(live_products)} of {result['expected_count']} approved draft(s) — see notes."
        )
    else:
        result["status"] = "error"
        result["error"] = "No approved draft could be published — see notes."
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Publishing is opt-in here too: a bare run resolves and validates but lists nothing.
    parser.add_argument("--live", action="store_true",
                        help="Actually publish the approved drafts. Without this it is a dry run.")
    parser.add_argument("--draft-id", default="",
                        help="Publish only this draft ID (it must still be approved in the sheet).")
    parser.add_argument("--no-email", action="store_true", help="Do not send the result email.")
    args = parser.parse_args()

    try:
        result = run(live=args.live, only_draft_id=args.draft_id.strip())
    except Exception as exc:  # noqa: BLE001 - top-level guard so we always report
        result = {"date": _now().date().isoformat(), "status": "error", "products": [],
                  "listed_count": 0, "expected_count": 0, "error": f"Unhandled error: {exc}"}

    try:
        import spend

        result["spend"] = spend.totals()
    except Exception:  # noqa: BLE001
        pass

    if args.live and not args.no_email and result.get("status") != "noop":
        try:
            notify.send(result)
        except notify.NotifyError as exc:
            print(json.dumps({"status": "email_failed", "error": str(exc)}), file=sys.stderr)

    for note in (result.get("notes") or [])[:40]:
        print("NOTE:", note)
    print("MODE:", "LIVE — approved drafts published" if args.live else "DRY RUN — nothing was listed")
    print(json.dumps({
        "status": result.get("status"),
        "listed": result.get("listed_count", 0),
        "approved": result.get("expected_count", 0),
        "error": result.get("error", ""),
    }))
    if result.get("status") in {"listed", "noop"}:
        return 0
    return 0 if not args.live else 1


if __name__ == "__main__":
    raise SystemExit(main())
