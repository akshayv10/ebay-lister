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


def _drift_percent(stored: Decimal, current: Decimal) -> Decimal:
    return (current - stored) / stored * Decimal("100")


def _refresh_variants(source: dict[str, Any], product_id: str) -> tuple[list[str], list[str]]:
    """Re-cost each variation from its current AliExpress SKU.

    Returns ``(blockers, notes)`` and rewrites ``visible_item_price`` /
    ``delivered_total`` in place. The refresh is unconditional: a hand-set price must be
    validated against today's cost, not the cost we drafted at, or an override set when
    the item was cheap silently becomes a below-cost listing.
    """
    variants = source.get("selected_variants") or []
    blockers: list[str] = []
    notes: list[str] = []
    try:
        _, fresh = ali_api.variant_records(product_id)
    except Exception as exc:  # noqa: BLE001 - no seller token or a transient API error
        return blockers, [f"Could not re-check per-variation costs ({exc})."]
    by_id = {str(record.get("id", "")): record for record in fresh or []}
    if not by_id:
        return blockers, ["AliExpress returned no variations to re-check."]

    for variant in variants:
        variant_id = str(variant.get("id", ""))
        current_record = by_id.get(variant_id)
        if current_record is None:
            blockers.append(f"Variation '{variant_id}' is no longer offered on AliExpress.")
            continue
        stored = _decimal(variant.get("delivered_total"))
        current = _decimal(current_record.get("delivered_total"))
        if stored is None or current is None or stored <= 0:
            continue
        drift = _drift_percent(stored, current)
        if drift > MAX_COST_DRIFT_PCT:
            blockers.append(
                f"Variation '{variant_id}' delivered cost rose {drift:.1f}% "
                f"(USD {stored:.2f} → USD {current:.2f})."
            )
        variant["visible_item_price"] = str(current_record.get("visible_item_price", variant["visible_item_price"]))
        variant["delivered_total"] = str(current_record.get("delivered_total", variant["delivered_total"]))
        if abs(drift) >= Decimal("0.5"):
            notes.append(f"Variation '{variant_id}' cost moved {drift:+.1f}%.")
    return blockers, notes


def cost_check(source: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Re-price the draft against AliExpress right now.

    Returns ``(ok, message, refreshed_source)``. A draft can sit in the sheet for days,
    so we refuse to list one whose delivered cost has risen beyond
    DRAFT_MAX_COST_DRIFT_PCT — the computed eBay price would no longer earn its margin.
    A cost that fell is fine: the source is refreshed so the price recomputes downward.

    The refreshed cost matters even when the reviewer set an explicit price, because
    normalize_source validates that price against ``delivered_total``. Leaving a stale
    cost there would let an override that was profitable at draft time list below cost.

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

    if len(variants) > 1:
        blockers, notes = _refresh_variants(source, product_id)
        if blockers:
            return False, " ".join(blockers) + " Re-draft this product.", source
        return True, " ".join(notes), source

    shipping = ali_api.freight(product_id, flat.get("sku_id", ""), price)
    current = ali_api.delivered_total(price, shipping)
    drift = _drift_percent(stored, current)
    if drift > MAX_COST_DRIFT_PCT:
        return False, (
            f"Delivered cost rose {drift:.1f}% since drafting "
            f"(USD {stored:.2f} → USD {current:.2f}), above the "
            f"{MAX_COST_DRIFT_PCT:.0f}% limit. Re-draft or raise the limit."
        ), source

    message = ""
    if abs(drift) >= Decimal("0.5"):
        message = f"Delivered cost moved {drift:+.1f}% (USD {stored:.2f} → USD {current:.2f}); repriced."
    variants[0]["visible_item_price"] = f"{price:.2f}"
    variants[0]["delivered_total"] = f"{current:.2f}"
    return True, message, source


def prepare_for_publish(
    draft: dict[str, Any], edits: dict[str, Any]
) -> tuple[bool, dict[str, Any], str]:
    """Resolve an approved draft into the exact source that would be listed.

    Applies the reviewer's sheet edits, re-costs against AliExpress, and validates the
    result. Returns ``(ok, merged_source, message)``.

    Deliberately side-effect free — no draft is marked, nothing is written — so the
    dry-run mode can report precisely what live mode would do, and so the merged source
    is available to persist on failure.
    """
    merged = draft_sheet.apply_edits(draft, edits)
    ok, message, merged = cost_check(merged)
    if not ok:
        return False, merged, message
    try:
        # Validate the edited source before touching eBay, so a typo in the sheet is a
        # clear message rather than a half-created listing. Keep the normalized result:
        # it carries the final prices, so the draft record and the listing agree.
        merged = normalize_source(merged)
    except (EbayError, ValueError) as exc:
        return False, merged, f"Edited draft is invalid: {exc}"
    return True, merged, message


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

    ok, merged, message = prepare_for_publish(draft, edits)
    if message:
        outcome["notes"].append(message)
    if not ok:
        # Persist the merged source even though it failed: the reviewer's edits are what
        # they will want to correct, and the sheet row is rebuilt from this record.
        draft_store.mark(draft, draft_store.STATUS_BLOCKED, publish_error=message, source=merged)
        outcome.update({"status": "blocked", "error": message})
        return outcome

    product_dir = run_root / draft_id / "product-1"
    product_dir.mkdir(parents=True, exist_ok=True)
    source_path = product_dir / "source.json"
    write_json(source_path, merged)

    # Keep the reviewed source on the draft from here on, so a failure below leaves the
    # reviewer's edits intact for them to correct and retry.
    draft_store.mark(draft, draft_store.STATUS_PUBLISHING, publish_error="", source=merged)
    try:
        # enrich=False: this source has already been enriched and then hand-edited.
        product = list_one(client, config, source_path, enrich=False)
    except (EbayError, OSError, ValueError) as exc:
        draft_store.mark(
            draft, draft_store.STATUS_PUBLISH_FAILED, publish_error=str(exc), source=merged
        )
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
    # The reviewer's tick, carried through the post-run refresh so a failed publish does
    # not erase their approval along with their edits.
    ticks: dict[str, str] = {}
    for row in approved:
        draft_id = row["draft_id"]
        if only_draft_id and draft_id != only_draft_id:
            continue
        values = row["values"]
        column = draft_sheet.COL["Publish?"]
        ticks[draft_id] = str(values[column]) if column < len(values) else "YES"
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
        # Run the real resolution — edits, cost re-check, validation — so the dry run
        # surfaces a bad category, a malformed variant, a below-cost override or a stale
        # supplier cost here, instead of the reviewer discovering it mid-publish.
        # prepare_for_publish writes nothing, so no draft changes state.
        valid = 0
        for draft, edits in selected:
            ok, merged, message = prepare_for_publish(draft, edits)
            variant = (merged.get("selected_variants") or [{}])[0]
            if ok:
                valid += 1
            result["products"].append({
                "title": merged.get("listing_title", ""),
                "aliexpress_url": merged.get("aliexpress_url", ""),
                "price": variant.get("visible_item_price", ""),
                "ebay_price": variant.get("expected_ebay_price", ""),
                "ebay_url": "",
                "reason": "would publish" if ok else message,
            })
            if message:
                result["notes"].append(f"{draft.get('draft_id', '')}: {message}")
        result["status"] = "partial"
        result["error"] = (
            f"DRY RUN — {valid} of {len(selected)} approved draft(s) would publish; "
            f"{len(selected) - valid} would be refused. Nothing was listed."
        )
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
    # right next to what was reviewed. The drafts now carry the merged reviewed source,
    # so this rewrites the row with the reviewer's edits rather than reverting them.
    sheet_update = draft_sheet.sync_drafts(
        [draft for draft, _ in selected], publish_cells=ticks
    )
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
