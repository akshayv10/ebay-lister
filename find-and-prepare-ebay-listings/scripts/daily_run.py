#!/usr/bin/env python3
"""Unattended daily entry point: pick the niche, source two AliExpress products
via the API, list both live on eBay, and email the result.

Run:
    python daily_run.py            # full run (sources, lists live, emails)
    python daily_run.py --dry-run  # sources + validates + prints email; no eBay, no email

Environment (see .github/workflows/daily.yml and README):
    RUN_TZ (default Asia/Kolkata), HISTORY_PATH (default state/resale-product-history.jsonl),
    RUNS_DIR (default ebay-listing-runs), EBAY_ACCOUNT_CONFIG, eBay + AliExpress + email secrets.
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


def run(dry_run: bool) -> dict[str, Any]:
    now = _now()
    local_date = now.date().isoformat()
    run_stamp = now.strftime("%Y%m%dT%H%M%S")
    history = load_history(HISTORY_PATH)
    niche = choose_niche(history, now.date())
    run_dir = RUNS_DIR / run_stamp

    result: dict[str, Any] = {
        "date": local_date, "niche": niche, "run_stamp": run_stamp,
        "status": "error", "products": [], "listed_count": 0, "notes": [],
    }

    sources, notes = ali_api.source_products(niche, run_stamp, local_date, history, needed=2)
    result["notes"] = notes
    result["products"] = product_summaries(sources)

    if len(sources) < 2:
        result["status"] = "partial" if sources else "error"
        result["error"] = f"Sourced only {len(sources)} of 2 qualifying products for niche '{niche}'."
        for summary in result["products"]:
            summary["reason"] = "second product not found; nothing listed"
        return result

    source_paths = write_sources(run_dir, sources)
    result["run_dir"] = str(run_dir)

    if dry_run:
        result["status"] = "partial"
        result["error"] = "DRY RUN — sources written and validated; eBay listing skipped."
        for summary in result["products"]:
            summary["reason"] = "dry run"
        return result

    # Unattended: fill any category-required item specifics we did not source.
    os.environ.setdefault("EBAY_AUTOFILL_REQUIRED_ASPECTS", "1")
    # Imported here so --dry-run never touches eBay credentials/imports.
    from ebay_listing import prepare, publish
    from ebay_common import EbayClient

    client = EbayClient()
    try:
        prepared = prepare(run_dir, client)
        run_id = str(prepared.get("run_id", ""))
        publish(run_dir, run_id, client, HISTORY_PATH)
    except (EbayError, OSError, ValueError) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        # Surface any per-product blocked reason written by prepare().
        for index, summary in enumerate(result["products"], 1):
            blocked = run_dir / f"product-{index}" / "result.json"
            if blocked.exists():
                try:
                    summary["reason"] = read_json(blocked).get("blocked_reason", "listing failed")
                except (EbayError, OSError, ValueError):
                    summary["reason"] = "listing failed"
            else:
                summary["reason"] = "listing failed"
        return result

    run_result = read_json(run_dir / "run-result.json")
    listed = attach_live_urls(result["products"], run_result)
    result["listed_count"] = listed
    result["status"] = "listed" if listed == 2 else "error"
    if listed != 2:
        result["error"] = f"Publish reported {listed} of 2 live listings; check run-result.json."
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

    print(json.dumps({"status": result.get("status"), "listed": result.get("listed_count", 0),
                      "niche": result.get("niche", ""), "error": result.get("error", "")}))
    return 0 if result.get("status") == "listed" else (0 if args.dry_run else 1)


if __name__ == "__main__":
    raise SystemExit(main())
