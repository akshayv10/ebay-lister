#!/usr/bin/env python3
"""Unattended daily entry point: pick the niche, source two AliExpress products
via the API, list both live on eBay, and email the result.

Run:
    python daily_run.py            # full run (sources, lists live, emails)
    python daily_run.py --dry-run  # sources + validates + prints email; no eBay, no email

Environment (see .github/workflows/daily.yml and README):
    RUN_TZ (default Asia/Kolkata), HISTORY_PATH (default state/resale-product-history.jsonl),
    RUNS_DIR (default ebay-listing-runs), EBAY_ACCOUNT_CONFIG, eBay + AliExpress +
    email + Google Sheets secrets.
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
from daily_history import choose_niche, load_history
from listing_job import normalize_source
from ebay_common import EbayError, read_json, write_json

RUN_TZ = os.environ.get("RUN_TZ", "Asia/Kolkata")
HISTORY_PATH = Path(os.environ.get("HISTORY_PATH", "state/resale-product-history.jsonl"))
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "ebay-listing-runs"))


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
            "ebay_url": product.get("ebay_url", ""),
            "listing_id": product.get("listing_id", ""),
        })
    return out


def run(dry_run: bool) -> dict[str, Any]:
    now = _now()
    local_date = now.date().isoformat()
    run_stamp = now.strftime("%Y%m%dT%H%M%S")
    history = load_history(HISTORY_PATH)
    niche = choose_niche(history, now.date())
    run_dir = RUNS_DIR / run_stamp
    pool = int(os.environ.get("ALI_SOURCE_POOL", "6"))

    result: dict[str, Any] = {
        "date": local_date, "niche": niche, "run_stamp": run_stamp,
        "status": "error", "products": [], "listed_count": 0, "notes": [],
    }

    # Over-source a small pool so one bad candidate doesn't sink the day.
    sources, notes = ali_api.source_products(niche, run_stamp, local_date, history, needed=pool)
    result["notes"] = list(notes)

    if not sources:
        result["error"] = f"Sourced 0 qualifying products for niche '{niche}'."
        return result

    write_sources(run_dir, sources)
    result["run_dir"] = str(run_dir)

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
    parser.add_argument("--dry-run", action="store_true",
                        help="Source and validate only; do not touch eBay or send email.")
    parser.add_argument("--no-email", action="store_true", help="Do not send the email (still runs eBay).")
    args = parser.parse_args()

    try:
        result = run(args.dry_run)
    except Exception as exc:  # noqa: BLE001 - top-level guard so we always email a failure
        result = {"date": _now().date().isoformat(), "status": "error",
                  "products": [], "listed_count": 0, "error": f"Unhandled error: {exc}"}

    # Running OpenAI spend (ledger lives in state/, committed back by the workflow).
    try:
        import spend

        result["spend"] = spend.totals()
    except Exception:  # noqa: BLE001
        pass

    # Persist the summary next to the run for auditing / the workflow commit-back.
    summary_dir = RUNS_DIR / str(result.get("run_stamp", "last"))
    try:
        summary_dir.mkdir(parents=True, exist_ok=True)
        write_json(summary_dir / "summary.json", result)
    except OSError:
        pass

    if not args.dry_run and not args.no_email:
        try:
            notify.send(result)
        except notify.NotifyError as exc:
            print(json.dumps({"status": "email_failed", "error": str(exc)}), file=sys.stderr)

    for note in (result.get("notes") or [])[:40]:
        print("NOTE:", note)
    print(json.dumps({"status": result.get("status"), "listed": result.get("listed_count", 0),
                      "niche": result.get("niche", ""), "error": result.get("error", "")}))
    return 0 if result.get("status") == "listed" else (0 if args.dry_run else 1)


if __name__ == "__main__":
    raise SystemExit(main())
