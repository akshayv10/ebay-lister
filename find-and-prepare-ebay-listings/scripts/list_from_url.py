#!/usr/bin/env python3
"""On-demand listing: feed one AliExpress product URL, list it live on eBay, email the result.

This is the manual counterpart to ``daily_run.py``. Instead of the daily niche rotation
picking two bestsellers, you hand it a single link you found yourself. It reuses the exact
same listing chain (``ali_api.get_product_detail`` -> ``product_to_source`` ->
``ebay_listing.list_resilient`` -> ``notify.send``).

Quality gates are advisory here: because *you* picked the product, a gate failure is
reported as a warning in the reply email rather than blocking the listing (only hard
failures — no id/title/price/images — stop it, since those can't be listed at all).

Run:
    python list_from_url.py <URL>            # dry run: fetch + validate, no eBay, no email
    python list_from_url.py <URL> --live     # actually publish to eBay + email the result
    python list_from_url.py <URL> --live --no-email

Environment: same as daily_run.py / the workflow (eBay + AliExpress + email + Sheets).
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
from listing_job import normalize_source
from ebay_common import EbayError, write_json

RUN_TZ = os.environ.get("RUN_TZ", "Asia/Kolkata")
HISTORY_PATH = Path(os.environ.get("HISTORY_PATH", "state/resale-product-history.jsonl"))
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "ebay-listing-runs"))


def _now() -> datetime:
    return datetime.now(ZoneInfo(RUN_TZ))


def build_source(url: str, run_stamp: str, local_date: str) -> tuple[dict[str, Any], str | None]:
    """Fetch the AliExpress product behind ``url`` and build a validated source.json dict.

    Returns ``(source, gate_warning)`` where ``gate_warning`` is None when the product
    passes every quality gate, else a short human reason (the listing still proceeds).
    Raises AliError for a bad link or a product that cannot be listed at all."""
    product_id = ali_api.product_id_from_url(url)
    detail = ali_api.get_product_detail(product_id)
    flat = ali_api.flatten_detail(detail)
    warning = ali_api.gate_reason(flat)
    source = ali_api.product_to_source(
        flat, niche="on-demand", run_stamp=run_stamp, local_date=local_date, enforce_gates=False
    )
    # Fail early on any schema problem, before eBay — mirrors daily_run.write_sources.
    normalize_source(source)
    return source, warning


def summary_from_source(source: dict[str, Any]) -> dict[str, Any]:
    variant = (source.get("selected_variants") or [{}])[0]
    return {
        "product_id": source["product_id"],
        "title": source["listing_title"],
        "aliexpress_url": source["aliexpress_url"],
        "price": variant.get("visible_item_price", ""),
        "ebay_url": "",
        "listing_id": "",
    }


def list_one(url: str, live: bool) -> dict[str, Any]:
    """List a single URL. Returns a daily_run-shaped result dict (for notify/sheet_sync)."""
    now = _now()
    local_date = now.date().isoformat()
    run_stamp = now.strftime("%Y%m%dT%H%M%S")
    run_dir = RUNS_DIR / f"ondemand-{run_stamp}"

    result: dict[str, Any] = {
        "date": local_date, "niche": "on-demand", "run_stamp": f"ondemand-{run_stamp}",
        "status": "error", "products": [], "listed_count": 0, "notes": [],
        "source_url": url,
    }

    try:
        source, warning = build_source(url, run_stamp, local_date)
    except (ali_api.AliError, ValueError) as exc:
        result["error"] = f"Could not prepare a listing from {url}: {exc}"
        return result

    if warning:
        result["notes"].append(f"Quality-gate warning (listing anyway): {warning}")

    product_dir = run_dir / "product-1"
    product_dir.mkdir(parents=True, exist_ok=True)
    write_json(product_dir / "source.json", source)
    result["run_dir"] = str(run_dir)

    if not live:
        result["status"] = "partial"
        result["products"] = [summary_from_source(source)]
        result["products"][0]["reason"] = "dry run"
        result["error"] = "DRY RUN — candidate fetched and validated; eBay listing skipped."
        return result

    os.environ.setdefault("EBAY_AUTOFILL_REQUIRED_ASPECTS", "1")
    from ebay_listing import list_resilient
    from ebay_common import EbayClient

    client = EbayClient()
    try:
        run_result = list_resilient(run_dir, client, needed=1, history_path=HISTORY_PATH)
    except (EbayError, OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result

    listed = run_result.get("products", [])
    result["products"] = [{
        "product_id": p.get("product_id"),
        "title": p.get("listing_title"),
        "aliexpress_url": p.get("aliexpress_url"),
        "price": (p.get("selected_variants") or [{}])[0].get("visible_item_price", ""),
        "ebay_url": p.get("ebay_url", ""),
        "listing_id": p.get("listing_id", ""),
    } for p in listed]
    result["listed_count"] = int(run_result.get("listed_count", len(listed)))
    result["notes"] += run_result.get("errors", [])

    try:
        import sheet_sync

        result["sheet_sync"] = sheet_sync.sync_products(listed)
    except Exception as exc:  # noqa: BLE001 - listing success must survive tracking failures
        result["sheet_sync"] = {
            "status": "queued", "written": 0, "queued": len(listed),
            "error": f"Could not prepare Google Sheets sync: {exc}",
        }

    if result["listed_count"] >= 1:
        result["status"] = "listed"
    else:
        result["error"] = "Listing failed — see notes."
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="AliExpress product-page URL to list")
    parser.add_argument("--live", action="store_true",
                        help="Actually publish to eBay. Without this, the run is a dry run.")
    parser.add_argument("--no-email", action="store_true", help="Do not send the reply email.")
    args = parser.parse_args()

    try:
        result = list_one(args.url, live=args.live)
    except Exception as exc:  # noqa: BLE001 - top-level guard so we always email a failure
        result = {"date": _now().date().isoformat(), "status": "error", "products": [],
                  "listed_count": 0, "error": f"Unhandled error: {exc}", "source_url": args.url}

    try:
        import spend

        result["spend"] = spend.totals()
    except Exception:  # noqa: BLE001
        pass

    summary_dir = RUNS_DIR / str(result.get("run_stamp", "last-ondemand"))
    try:
        summary_dir.mkdir(parents=True, exist_ok=True)
        write_json(summary_dir / "summary.json", result)
    except OSError:
        pass

    if args.live and not args.no_email:
        try:
            notify.send(result)
        except notify.NotifyError as exc:
            print(json.dumps({"status": "email_failed", "error": str(exc)}), file=sys.stderr)

    for note in (result.get("notes") or [])[:40]:
        print("NOTE:", note)
    print("MODE:", "DRY RUN — nothing was listed" if not args.live else "LIVE — listing published")
    print(json.dumps({"status": result.get("status"), "listed": result.get("listed_count", 0),
                      "error": result.get("error", "")}))
    return 0 if result.get("status") in ("listed", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
