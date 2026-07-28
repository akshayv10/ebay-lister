#!/usr/bin/env python3
"""Publish prepared drafts to eBay — the button half of the flow.

``daily_run.py --draft`` sources, enriches and validates two products each day and parks
them as drafts. Nothing goes live on a schedule. When you are ready, this publishes
**the most recent day's batch**, and reports any older drafts still waiting rather than
sweeping them up, so one press is a predictable two listings.

Per draft it re-checks the AliExpress cost (a draft may have sat for days), then lists
through the unchanged ``ebay_listing.list_one`` path — prepare, publish, 10% General
promotion — with enrichment disabled so the copy that goes live is the copy that was
drafted. Then history, the Auto Lister tab, the status board, and an email.

Each draft is independent: one failure never blocks the others. A draft eBay refuses is
retried once more and then parked, so a product eBay will never accept — a branded
medical device listed as Unbranded, say — stops reappearing in every result.

Selection is read from the draft records on disk, never from the sheet, so a Sheets
outage cannot stop a publish.

Run:
    python publish_drafts.py                      # dry run of the newest batch
    python publish_drafts.py --live               # publish the newest batch
    python publish_drafts.py --live --all         # clear the whole backlog
    python publish_drafts.py --live --run-stamp <stamp>   # one specific batch
    python publish_drafts.py --live --draft-id <id>       # one draft, even if parked

Environment: the same eBay / AliExpress / email / Sheets configuration as daily_run.py,
plus DRAFT_MAX_COST_DRIFT_PCT (default 10) and DRAFT_MAX_PUBLISH_ATTEMPTS (default 2).
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
from ebay_common import ApiError, EbayError, UnknownOutcome, write_json
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


# eBay statuses that mean "try again later", not "this product is unacceptable".
_RETRYABLE_STATUSES = {
    401,  # token expired or auth outage — our credentials, not the listing
    403,  # permission problem — likewise
    408,  # request timeout
    429,  # rate limited
}


def is_permanent_rejection(exc: Exception) -> bool:
    """True only when eBay has refused this particular listing on its merits.

    Only such a refusal should consume a parking attempt. A timeout, a 5xx, a rate
    limit, an auth outage or a local OSError says nothing about the product — counting
    those would let a brief eBay outage across two runs park an entire batch and
    exclude it from every later publish, including --all.
    """
    if isinstance(exc, UnknownOutcome):
        # The mutation may have reached eBay; the outcome is unknown, so it is neither a
        # confirmed rejection nor safe to treat as one.
        return False
    if isinstance(exc, ApiError):
        return 400 <= int(getattr(exc, "status", 0)) < 500 and exc.status not in _RETRYABLE_STATUSES
    # Anything else — plain EbayError from our own checks, OSError, ValueError — is not a
    # confirmed product rejection. Default to retryable.
    return False


def prepare_for_publish(draft: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    """Resolve a draft into the exact source that would be listed.

    Re-costs against AliExpress and validates. Returns ``(ok, merged_source, message)``.

    Deliberately side-effect free — no draft is marked, nothing is written — so dry-run
    can report precisely what a live run would do, and so the source is available to
    persist on failure.
    """
    merged = json.loads(json.dumps(draft.get("source", {}) or {}))
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
    client: Any,
    config: dict[str, Any],
    run_root: Path,
) -> dict[str, Any]:
    """Publish a single draft. Returns a result dict; never raises."""
    from ebay_listing import list_one

    draft_id = str(draft.get("draft_id", ""))
    outcome: dict[str, Any] = {"draft_id": draft_id, "status": "error", "notes": []}
    attempt = draft_store.attempts(draft) + 1

    ok, merged, message = prepare_for_publish(draft)
    if message:
        outcome["notes"].append(message)
    if not ok:
        # A cost or validation refusal is not eBay rejecting the product, so it does not
        # count towards the attempt limit — the draft is still worth retrying tomorrow.
        draft_store.mark(draft, draft_store.STATUS_BLOCKED, publish_error=message, source=merged)
        outcome.update({"status": "blocked", "error": message})
        return outcome

    product_dir = run_root / draft_id / "product-1"
    product_dir.mkdir(parents=True, exist_ok=True)
    source_path = product_dir / "source.json"
    write_json(source_path, merged)

    draft_store.mark(draft, draft_store.STATUS_PUBLISHING, publish_error="", source=merged)
    try:
        # enrich=False: this source was already enriched at draft time, and re-running the
        # AI here would rewrite copy that is about to go live.
        product = list_one(client, config, source_path, enrich=False)
    except (EbayError, OSError, ValueError) as exc:
        # Only a refusal of this listing on its merits burns an attempt. A timeout, 5xx,
        # rate limit or auth outage leaves the count alone so the draft stays retryable.
        permanent = is_permanent_rejection(exc)
        counted = attempt if permanent else draft_store.attempts(draft)
        parked = permanent and counted >= draft_store.MAX_PUBLISH_ATTEMPTS
        note = (
            f"attempt {counted} of {draft_store.MAX_PUBLISH_ATTEMPTS}"
            f"{' — parked' if parked else ''}"
            if permanent else "transient failure, not counted"
        )
        draft_store.mark(
            draft,
            draft_store.STATUS_PUBLISH_FAILED,
            publish_error=f"[{note}] {exc}",
            source=merged,
            publish_attempts=counted,
        )
        outcome.update({
            "status": "publish_failed", "error": str(exc),
            "attempt": counted, "parked": parked, "permanent": permanent,
        })
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


def _summaries(drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "draft_id": draft.get("draft_id", ""),
            "run_stamp": draft.get("run_stamp", ""),
            "created_at": draft.get("created_at", ""),
            "status": draft.get("status", ""),
            "attempts": draft_store.attempts(draft),
            "title": (draft.get("source", {}) or {}).get("listing_title", ""),
            "error": draft.get("publish_error", ""),
        }
        for draft in drafts
    ]


def select_drafts(
    *, only_draft_id: str = "", run_stamp: str = "", publish_all: bool = False
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Decide what this run publishes. Returns (drafts, batch_label, notes).

    Precedence: an explicit draft ID, then a named batch, then everything pending, then
    — the normal case — the newest batch only. Older drafts are deliberately left for a
    later run and reported instead, so one press publishes a predictable two listings
    rather than a week's accumulation.
    """
    notes: list[str] = []
    if only_draft_id:
        try:
            draft = draft_store.load(only_draft_id)
        except EbayError as exc:
            return [], "", [str(exc)]
        if not draft_store.is_publishable(draft):
            return [], "", [f"{only_draft_id}: status is '{draft.get('status')}' — not publishable"]
        if draft_store.is_parked(draft):
            notes.append(f"{only_draft_id} was parked after repeated failures; forcing it anyway.")
        return [draft], str(draft.get("run_stamp", "")), notes

    if run_stamp:
        drafts = [d for d in draft_store.batches().get(run_stamp, []) if draft_store.auto_selectable(d)]
        return drafts, run_stamp, notes

    if publish_all:
        drafts = [d for d in draft_store.pending() if draft_store.auto_selectable(d)]
        return drafts, "all", notes

    batch, drafts = draft_store.latest_batch()
    return drafts, batch, notes


def run(
    live: bool,
    only_draft_id: str = "",
    run_stamp: str = "",
    publish_all: bool = False,
) -> dict[str, Any]:
    now = _now()
    result: dict[str, Any] = {
        "date": now.date().isoformat(),
        "niche": "drafts",
        "status": "error",
        "expected_count": 0,
        "products": [],
        "listed_count": 0,
        "notes": [],
    }

    # Selection comes from the draft records on disk, not the sheet. The sheet is a
    # status board now, so a Sheets outage can never stop a publish.
    selected, batch, notes = select_drafts(
        only_draft_id=only_draft_id, run_stamp=run_stamp, publish_all=publish_all
    )
    result["notes"] += notes
    result["batch"] = batch

    # Parked drafts are reported separately, so keep the two lists disjoint — the same
    # product appearing under both headings reads like a bug.
    parked = [draft for draft in draft_store.pending() if draft_store.is_parked(draft)]
    parked_ids = {item.get("draft_id") for item in parked}
    remaining = [
        draft for draft in draft_store.backlog(exclude_run_stamp=batch)
        if draft.get("draft_id") not in parked_ids
    ]
    result["pending_count"] = len(remaining)
    result["pending"] = _summaries(remaining)
    result["parked"] = _summaries(parked)

    result["expected_count"] = len(selected)
    if not selected:
        result["status"] = "noop"
        result["error"] = (
            f"Nothing to publish. {len(remaining)} draft(s) pending, "
            f"{len(parked)} parked after repeated failures."
            if (remaining or parked) else
            "Nothing to publish — no drafts are waiting."
        )
        return result

    if not live:
        # Run the real resolution — cost re-check and validation — so a dry run surfaces
        # a stale supplier cost or an invalid source here rather than mid-publish.
        # prepare_for_publish writes nothing, so no draft changes state.
        valid = 0
        for draft in selected:
            ok, merged, message = prepare_for_publish(draft)
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
            f"DRY RUN — {valid} of {len(selected)} draft(s) in batch {batch} would "
            f"publish; {len(selected) - valid} would be refused. Nothing was listed."
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
    for draft in selected:
        outcome = publish_one(draft, client, config, run_root)
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

    # Refresh each touched row so the board shows the live link, or the failure and its
    # attempt count. Purely informational — nothing is ever read back from it.
    sheet_update = draft_sheet.sync_drafts(selected)
    if sheet_update.get("error"):
        result["notes"].append(f"Drafts sheet: {sheet_update['error']}")

    # Recompute after publishing so the report reflects what is genuinely still waiting.
    still_parked = [draft for draft in draft_store.pending() if draft_store.is_parked(draft)]
    parked_ids = {item.get("draft_id") for item in still_parked}
    result["parked"] = _summaries(still_parked)
    result["pending"] = _summaries([
        draft for draft in draft_store.backlog(exclude_run_stamp="")
        if draft.get("draft_id") not in parked_ids
    ])
    result["pending_count"] = len(result["pending"])

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
            f"Published {len(live_products)} of {result['expected_count']} draft(s) — see notes."
        )
    else:
        result["status"] = "error"
        result["error"] = "No draft could be published — see notes."
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Publishing is opt-in here too: a bare run resolves and validates but lists nothing.
    parser.add_argument("--live", action="store_true",
                        help="Actually publish. Without this it is a dry run.")
    parser.add_argument("--all", dest="publish_all", action="store_true",
                        help="Publish every pending draft, not just the newest batch.")
    parser.add_argument("--run-stamp", default="",
                        help="Publish one specific batch by its run stamp.")
    parser.add_argument("--draft-id", default="",
                        help="Publish only this draft, even if it has been parked.")
    parser.add_argument("--no-email", action="store_true", help="Do not send the result email.")
    args = parser.parse_args()

    try:
        result = run(
            live=args.live,
            only_draft_id=args.draft_id.strip(),
            run_stamp=args.run_stamp.strip(),
            publish_all=args.publish_all,
        )
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

    # The backlog is the whole point of publishing one batch at a time — say it plainly
    # rather than leaving it to be inferred from the sheet.
    for item in result.get("pending", []):
        print(f"PENDING: {item['run_stamp']}  {item['title'][:60]}")
    for item in result.get("parked", []):
        print(f"PARKED : {item['draft_id']}  after {item['attempts']} attempt(s) — {item['title'][:44]}")
    if result.get("pending"):
        print(f"\n{len(result['pending'])} draft(s) still pending. "
              "Run again to publish the next batch, or use --all to clear them.")

    print("MODE:", "LIVE — drafts published" if args.live else "DRY RUN — nothing was listed")
    print(json.dumps({
        "status": result.get("status"),
        "batch": result.get("batch", ""),
        "listed": result.get("listed_count", 0),
        "selected": result.get("expected_count", 0),
        "pending": result.get("pending_count", 0),
        "parked": len(result.get("parked", [])),
        "error": result.get("error", ""),
    }))
    if result.get("status") in {"listed", "noop"}:
        return 0
    return 0 if not args.live else 1


if __name__ == "__main__":
    raise SystemExit(main())
