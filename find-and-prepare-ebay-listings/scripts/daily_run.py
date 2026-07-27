#!/usr/bin/env python3
"""Unattended daily entry point: pick the niche, source two AliExpress products
via the API, then either draft them for review or list them live on eBay, and email
the result.

Run:
    python daily_run.py            # dry run (sources + validates; no eBay, no email)
    python daily_run.py --draft    # source, enrich, validate, save drafts for review
    python daily_run.py --live     # source and publish live immediately (automatic mode)

``--draft`` is the reviewed pathway: it produces drafts you edit in the Drafts sheet tab
and approve with the "Publish drafts" workflow. ``--live`` is the original automatic
pathway, kept intact so the schedule can be switched back to hands-off listing at any
time (repository variable LISTING_MODE=auto).

Environment (see .github/workflows/daily.yml and README):
    RUN_TZ (default Asia/Kolkata), HISTORY_PATH (default state/resale-product-history.jsonl),
    RUNS_DIR (default ebay-listing-runs), DRAFT_DIR (default state/drafts),
    EBAY_ACCOUNT_CONFIG, eBay + AliExpress + email + Google Sheets secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import ali_api
import notify
from daily_history import load_history, niche_priority
from listing_job import normalize_source
from ebay_common import EbayError, read_json, write_json

RUN_TZ = os.environ.get("RUN_TZ", "Asia/Kolkata")
HISTORY_PATH = Path(os.environ.get("HISTORY_PATH", "state/resale-product-history.jsonl"))
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "ebay-listing-runs"))
PREVIEW_PATH = Path(os.environ.get("DRAFT_PREVIEW_PATH", "ebay-listing-runs/drafts-preview.html"))
SHEET_URL = os.environ.get("SHEETS_SPREADSHEET_URL", "")


def _now():
    return datetime.now(ZoneInfo(RUN_TZ))


def write_sources(run_dir: Path, sources: list[dict[str, Any]]) -> list[Path]:
    paths: list[Path] = []
    for index, source in enumerate(sources, 1):
        # normalize_source raises on any schema problem — fail early, before eBay.
        normalize_source(source)
        product_dir = run_dir / f"product-{index}"
        product_dir.mkdir(parents=True, exist_ok=True)
        source_path = product_dir / "source.json"
        write_json(source_path, source)
        paths.append(source_path)
    return paths


def product_summaries(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for source in sources:
        variant = (source.get("selected_variants") or [{}])[0]
        summaries.append({
            "product_id": source["product_id"],
            "title": source["listing_title"],
            "aliexpress_url": source["aliexpress_url"],
            "price": variant.get("visible_item_price", ""),
            "ebay_url": "",
            "listing_id": "",
        })
    return summaries


def attach_live_urls(summaries: list[dict[str, Any]], run_result: dict[str, Any]) -> int:
    """Copy live eBay URLs from the published run-result onto the summaries."""
    by_id = {str(p.get("product_id")): p for p in run_result.get("products", [])}
    listed = 0
    for summary in summaries:
        product = by_id.get(str(summary["product_id"]))
        if product and product.get("ebay_url"):
            summary["ebay_url"] = product["ebay_url"]
            summary["listing_id"] = product.get("listing_id", "")
            listed += 1
    return listed


def listed_summaries(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for product in products:
        variant = (product.get("selected_variants") or [{}])[0]
        out.append({
            "product_id": product.get("product_id"),
            "title": product.get("listing_title"),
            "aliexpress_url": product.get("aliexpress_url"),
            "price": variant.get("visible_item_price", ""),
            "ebay_price": variant.get("expected_ebay_price", ""),
            "ebay_url": product.get("ebay_url", ""),
            "listing_id": product.get("listing_id", ""),
        })
    return out


def build_drafts(run_dir: Path, source_paths: list[Path], run_stamp: str) -> dict[str, Any]:
    """Enrich each sourced product, validate it against eBay read-only, and save it as a
    draft for review. Creates nothing on eBay — no images are uploaded, no inventory item
    or offer is written. The reviewer publishes later via publish_drafts.py."""
    import draft_sheet
    import draft_store
    from ebay_common import EbayClient
    from ebay_listing import enrich_source_with_ai, enrich_source_with_variants, validate_for_draft

    # Match what the publish step will do, so what you review is what eBay will accept.
    os.environ.setdefault("EBAY_AUTOFILL_REQUIRED_ASPECTS", "1")

    drafts: list[dict[str, Any]] = []
    notes: list[str] = []
    client: Any = None
    try:
        client = EbayClient()
    except Exception as exc:  # noqa: BLE001 - validation is advisory; never lose a draft over it
        notes.append(f"eBay validation unavailable ({exc}); drafts saved without category checks.")

    for source_path in source_paths:
        product_notes: list[str] = []
        # Enrichment happens here, not at publish time, so the draft you review carries the
        # final AI copy and real variants — and publishing never overwrites your edits.
        try:
            enrich_source_with_variants(source_path)
        except Exception as exc:  # noqa: BLE001 - enrichment must never block a draft
            product_notes.append(f"Variant enrichment skipped: {exc}")
        try:
            enrich_source_with_ai(source_path)
        except Exception as exc:  # noqa: BLE001
            product_notes.append(f"AI copy skipped: {exc}")

        try:
            source = normalize_source(read_json(source_path))
        except (EbayError, ValueError) as exc:
            notes.append(f"{source_path.parent.name}: unusable source ({exc})")
            continue

        validation: dict[str, Any] = {}
        if client is not None:
            validation = validate_for_draft(client, source)

        try:
            spare = ali_api.spare_images(source["product_id"], source.get("source_images", []))
        except Exception as exc:  # noqa: BLE001 - advisory only
            spare = []
            product_notes.append(f"Spare-image lookup skipped: {exc}")

        draft = draft_store.new_draft(
            source,
            run_stamp=run_stamp,
            validation=validation,
            spare_images=spare,
            notes=product_notes,
        )
        draft_store.save(draft)
        drafts.append(draft)

    sheet = draft_sheet.sync_drafts(drafts)
    if sheet.get("error"):
        notes.append(f"Drafts sheet: {sheet['error']}")

    # The preview is both the emailed body and the uploaded workflow artifact.
    preview_html = ""
    try:
        import draft_preview

        preview_html = draft_preview.render(drafts, sheet_url=SHEET_URL, date=run_stamp)
        PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW_PATH.write_text(preview_html, encoding="utf-8")
    except OSError as exc:
        notes.append(f"Could not write the preview page: {exc}")

    return {
        "drafts": drafts,
        "draft_count": len(drafts),
        "draft_sheet": sheet,
        "preview_html": preview_html,
        "preview_path": str(PREVIEW_PATH),
        "notes": notes,
    }


def run(mode: str) -> dict[str, Any]:
    dry_run = mode != "live"
    now = _now()
    local_date = now.date().isoformat()
    run_stamp = now.strftime("%Y%m%dT%H%M%S")
    history = load_history(HISTORY_PATH)
    niche_order = niche_priority(history, now.date())
    run_dir = RUNS_DIR / run_stamp
    pool = int(os.environ.get("ALI_SOURCE_POOL", "6"))

    result: dict[str, Any] = {
        "date": local_date, "niche": niche_order[0], "run_stamp": run_stamp,
        "status": "error", "products": [], "listed_count": 0, "notes": [],
    }

    # Niche assignment is a preference, not a hard daily lock: over-source a small pool
    # per niche, and if the preferred niche's feed sources 0 qualifying candidates, fall
    # back to the next niche in priority order rather than failing the whole run.
    notes: list[str] = []
    sources: list[dict[str, Any]] = []
    niche = niche_order[0]
    for candidate_niche in niche_order:
        niche = candidate_niche
        candidate_sources, candidate_notes = ali_api.source_products(
            candidate_niche, run_stamp, local_date, history, needed=pool
        )
        notes += [f"[{candidate_niche}] {note}" for note in candidate_notes]
        if candidate_sources:
            sources = candidate_sources
            break
        notes.append(f"niche '{candidate_niche}' sourced 0 candidates — trying next niche")

    result["niche"] = niche
    result["notes"] = notes

    if not sources:
        result["error"] = f"Sourced 0 qualifying products across all {len(niche_order)} niches."
        return result

    source_paths = write_sources(run_dir, sources)
    result["run_dir"] = str(run_dir)

    if mode == "draft":
        drafted = build_drafts(run_dir, source_paths[:2], run_stamp)
        result.update({
            key: drafted[key] for key in
            ("drafts", "draft_count", "draft_sheet", "preview_html", "preview_path")
        })
        result["notes"] += drafted["notes"]
        result["mode"] = "draft"
        result["products"] = [
            {
                "product_id": draft["source"]["product_id"],
                "title": draft["source"]["listing_title"],
                "aliexpress_url": draft["source"]["aliexpress_url"],
                "price": (draft["source"].get("selected_variants") or [{}])[0].get("visible_item_price", ""),
                "ebay_price": (draft["source"].get("selected_variants") or [{}])[0].get("expected_ebay_price", ""),
                "draft_id": draft["draft_id"],
                "ebay_url": "",
                "listing_id": "",
                "reason": "awaiting review",
            }
            for draft in drafted["drafts"]
        ]
        if drafted["draft_count"]:
            result["status"] = "drafted"
        else:
            result["status"] = "error"
            result["error"] = "Sourced products could not be drafted — see notes."
        return result

    if dry_run:
        result["status"] = "partial"
        result["error"] = f"DRY RUN — {len(sources)} candidate(s) sourced and validated; eBay listing skipped."
        result["products"] = product_summaries(sources[:2])
        for summary in result["products"]:
            summary["reason"] = "dry run"
        return result

    # Unattended: fill any category-required item specifics we did not source.
    os.environ.setdefault("EBAY_AUTOFILL_REQUIRED_ASPECTS", "1")
    from ebay_listing import list_resilient  # imported here so --dry-run never needs eBay creds
    from ebay_common import EbayClient

    client = EbayClient()
    try:
        run_result = list_resilient(run_dir, client, needed=2, history_path=HISTORY_PATH)
    except (EbayError, OSError, ValueError) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        return result

    listed = run_result.get("products", [])
    result["products"] = listed_summaries(listed)
    result["listed_count"] = int(run_result.get("listed_count", len(listed)))
    result["notes"] += run_result.get("errors", [])
    try:
        import sheet_sync

        result["sheet_sync"] = sheet_sync.sync_products(listed)
    except Exception as exc:  # noqa: BLE001 - listing success must survive tracking failures
        result["sheet_sync"] = {
            "status": "queued",
            "written": 0,
            "queued": len(listed),
            "error": f"Could not prepare Google Sheets sync: {exc}",
        }
    if result["listed_count"] >= 2:
        result["status"] = "listed"
    elif result["listed_count"] == 1:
        result["status"] = "partial"
        result["error"] = "Listed 1 of 2; the other candidate(s) failed — see notes."
    else:
        result["status"] = "error"
        result["error"] = "Listed 0 of 2; all candidates failed — see notes."
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Publishing is opt-in: a bare `python3 daily_run.py` must never create eBay listings,
    # so testing can't accidentally go live.
    parser.add_argument("--live", action="store_true",
                        help="Actually publish to eBay. Without this, the run is a dry run.")
    parser.add_argument("--draft", action="store_true",
                        help="Source, enrich and validate, then save drafts for review "
                             "instead of publishing. Nothing is created on eBay.")
    parser.add_argument("--dry-run", action="store_true",
                        help="No-op (dry run is the default); kept for backwards compatibility.")
    parser.add_argument("--no-email", action="store_true", help="Do not send the email (still runs eBay).")
    args = parser.parse_args()
    dry_run = not args.live
    # --live wins if both are passed, so an explicit publish request is never downgraded
    # into a draft by a stray flag.
    mode = "live" if args.live else ("draft" if args.draft else "dry-run")

    try:
        result = run(mode)
    except Exception as exc:  # noqa: BLE001 - top-level guard so we always email a failure
        result = {"date": _now().date().isoformat(), "status": "error",
                  "products": [], "listed_count": 0, "error": f"Unhandled error: {exc}"}

    # Running OpenAI spend (ledger lives in state/, committed back by the workflow).
    try:
        import spend

        result["spend"] = spend.totals()
    except Exception:  # noqa: BLE001
        pass

    # Persist the summary next to the run for auditing / the workflow commit-back. The
    # rendered preview and the full draft bodies are already on disk under state/drafts/
    # and the preview file, so keep them out of the summary rather than duplicating them.
    summary_dir = RUNS_DIR / str(result.get("run_stamp", "last"))
    try:
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary = {key: value for key, value in result.items() if key not in ("preview_html", "drafts")}
        write_json(summary_dir / "summary.json", summary)
    except OSError:
        pass

    if not args.no_email:
        try:
            if mode == "draft":
                notify.send_drafts(result)
            elif not dry_run:
                notify.send(result)
        except notify.NotifyError as exc:
            print(json.dumps({"status": "email_failed", "error": str(exc)}), file=sys.stderr)

    for note in (result.get("notes") or [])[:40]:
        print("NOTE:", note)
    mode_message = {
        "live": "LIVE — listings published",
        "draft": f"DRAFT — {result.get('draft_count', 0)} draft(s) saved for review; nothing was listed",
    }.get(mode, "DRY RUN — nothing was listed")
    print("MODE:", mode_message)
    print(json.dumps({"status": result.get("status"), "listed": result.get("listed_count", 0),
                      "drafted": result.get("draft_count", 0),
                      "niche": result.get("niche", ""), "error": result.get("error", "")}))
    if mode == "draft":
        return 0 if result.get("status") == "drafted" else 1
    return 0 if result.get("status") == "listed" else (0 if dry_run else 1)


if __name__ == "__main__":
    raise SystemExit(main())
