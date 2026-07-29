#!/usr/bin/env python3
"""List the AliExpress products *you* picked, from one blob of pasted text.

This is the Actions-button counterpart to ``list_from_url.py``. Instead of one URL per
email, you paste whatever you copied — one link or two, with or without surrounding
prose — and every AliExpress product link in it is listed. One link lists one product;
two list two. Nothing here decides *what* to sell: you already did that.

Quality gates are **advisory**, exactly as on the on-demand path. Because the products
come from you they are pre-approved, so a rating/reviews/orders/price miss is reported
as a warning in the report email rather than blocking the listing. Only a product that
cannot be listed at all (no id, title, price, or images) is refused, and refusing one
never stops its sibling.

Everything downstream is the existing pipeline, unchanged: ``ali_api.get_product_detail``
-> ``product_to_source`` -> ``ebay_listing.list_resilient`` (AI/variant enrichment,
category + aspects, EPS images, publish, 10% General promotion) -> ``sheet_sync`` ->
``notify.send``.

Run:
    python list_picked.py --links "<pasted text>"           # dry run: fetch + validate
    python list_picked.py --links "<pasted text>" --live    # publish to eBay + email
    pbpaste | python list_picked.py --live                  # links on stdin

Environment: same as daily_run.py / list_from_url.py (eBay + AliExpress + email +
Sheets). ``PICKED_MAX_PRODUCTS`` (default 5) caps how many links one run will accept.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import ali_api
import notify
from list_from_url import build_source, summary_from_source
from listing_job import JobError
from ebay_common import EbayError, write_json

RUN_TZ = os.environ.get("RUN_TZ", "Asia/Kolkata")
HISTORY_PATH = Path(os.environ.get("HISTORY_PATH", "state/resale-product-history.jsonl"))
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "ebay-listing-runs"))

# A stray bulk paste must not turn into a dozen live listings.
MAX_PRODUCTS = int(os.environ.get("PICKED_MAX_PRODUCTS", "5"))

# Candidate links inside free text: any aliexpress host followed by a path. Stops at
# whitespace and at the punctuation that typically *follows* a pasted URL rather than
# belonging to it.
_LINK_RE = re.compile(
    r"(?:https?://)?[\w.-]*\baliexpress\.[a-z]{2,}(?:\.[a-z]{2,})?/[^\s,;\"'<>()\[\]]*",
    re.IGNORECASE,
)
# Share links that carry no product id in the URL itself and must be followed first.
_SHORTENER_PATHS = ("/_", "/e/_")


class PickError(ValueError):
    """The pasted text yielded no usable set of products."""


def _now() -> datetime:
    return datetime.now(ZoneInfo(RUN_TZ))


def _resolve_redirect(url: str, timeout: float = 15.0) -> str:
    """Follow a mobile share link to the real product page. Returns the final URL, or
    the original when it cannot be resolved — the caller reports that as a skip."""
    request = urllib.request.Request(
        url if "//" in url else "https://" + url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.url or url
    except Exception:  # noqa: BLE001 - an unresolvable link is a skip, never a crash
        return url


def _looks_shortened(url: str) -> bool:
    path = urllib.parse.urlsplit(url if "//" in url else "https://" + url).path
    return any(path.startswith(prefix) for prefix in _SHORTENER_PATHS)


def extract_urls(text: str, *, follow_redirects: bool = True) -> tuple[list[str], list[str]]:
    """Pull every AliExpress product link out of ``text``.

    Returns ``(urls, notes)``: canonical ``/item/<id>.html`` URLs in paste order,
    deduplicated by product id, plus human notes about links that were skipped.
    Non-AliExpress links are ignored silently — you may paste context around the links.

    Raises PickError when nothing usable is found, or when more products are named than
    ``PICKED_MAX_PRODUCTS`` allows."""
    notes: list[str] = []
    seen: dict[str, str] = {}

    for raw in _LINK_RE.findall(text or ""):
        candidate = raw.rstrip(".,;:!?")
        try:
            product_id = ali_api.product_id_from_url(candidate)
        except ali_api.AliError:
            # Short share links (a.aliexpress.com/_xxx) hold no id until followed.
            if not (follow_redirects and _looks_shortened(candidate)):
                notes.append(
                    f"Skipped {candidate} — no AliExpress product id in it. "
                    "Paste the full /item/<id>.html link."
                )
                continue
            try:
                product_id = ali_api.product_id_from_url(_resolve_redirect(candidate))
            except ali_api.AliError:
                notes.append(
                    f"Skipped {candidate} — could not resolve that share link. "
                    "Open it and paste the full /item/<id>.html link."
                )
                continue
        if product_id in seen:
            continue
        seen[product_id] = ali_api.detail_url(product_id)

    urls = list(seen.values())
    if not urls:
        raise PickError(
            "No AliExpress product links found in the text. Paste at least one link "
            "like https://www.aliexpress.us/item/1005006000000001.html"
            + (" (" + notes[0] + ")" if notes else "")
        )
    if len(urls) > MAX_PRODUCTS:
        raise PickError(
            f"Found {len(urls)} products but this run accepts at most {MAX_PRODUCTS}. "
            "Paste fewer links, or raise PICKED_MAX_PRODUCTS."
        )
    return urls, notes


def list_picked(text: str, live: bool, *, follow_redirects: bool = True) -> dict[str, Any]:
    """List every product named in ``text``. Returns a daily_run-shaped result dict
    (for notify / sheet_sync). Never raises for a bad link — that degrades to an error
    field or a per-product note so the report always gets sent."""
    now = _now()
    local_date = now.date().isoformat()
    run_stamp = now.strftime("%Y%m%dT%H%M%S")
    run_dir = RUNS_DIR / f"picked-{run_stamp}"

    result: dict[str, Any] = {
        "date": local_date, "niche": "picked", "run_stamp": f"picked-{run_stamp}",
        "status": "error", "products": [], "listed_count": 0, "notes": [],
        "run_dir": str(run_dir), "expected_count": 0,
    }

    try:
        urls, notes = extract_urls(text, follow_redirects=follow_redirects)
    except PickError as exc:
        result["error"] = str(exc)
        return result

    result["notes"] += notes
    result["expected_count"] = len(urls)
    result["source_url"] = urls[0] if len(urls) == 1 else ", ".join(urls)

    prepared: list[dict[str, Any]] = []
    for index, url in enumerate(urls, 1):
        try:
            source, warning = build_source(url, run_stamp, local_date)
        except (ali_api.AliError, JobError, ValueError) as exc:
            # One unusable link must never cost you the other product.
            result["notes"].append(f"Could not prepare a listing from {url}: {exc}")
            continue
        if warning:
            result["notes"].append(
                f"Quality-gate warning (listing anyway) for {url}: {warning}"
            )
        product_dir = run_dir / f"product-{index}"
        product_dir.mkdir(parents=True, exist_ok=True)
        write_json(product_dir / "source.json", source)
        prepared.append(source)

    if not prepared:
        result["error"] = "None of the pasted links could be prepared — see notes."
        return result

    if not live:
        result["status"] = "partial"
        result["products"] = [summary_from_source(source) for source in prepared]
        for product in result["products"]:
            product["reason"] = "dry run"
        result["error"] = (
            f"DRY RUN — {len(prepared)} candidate(s) fetched and validated; "
            "eBay listing skipped."
        )
        return result

    os.environ.setdefault("EBAY_AUTOFILL_REQUIRED_ASPECTS", "1")
    from ebay_listing import list_resilient
    from ebay_common import EbayClient

    client = EbayClient()
    try:
        run_result = list_resilient(
            run_dir, client, needed=len(prepared), history_path=HISTORY_PATH
        )
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

    if result["listed_count"] >= len(prepared):
        result["status"] = "listed"
    elif result["listed_count"]:
        result["status"] = "partial"
        result["error"] = (
            f"Listed {result['listed_count']} of {len(prepared)} — see notes."
        )
    else:
        result["error"] = "Listing failed — see notes."
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--links", default=None,
                        help="Pasted text containing one or more AliExpress product "
                             "links. Read from stdin when omitted.")
    parser.add_argument("--live", action="store_true",
                        help="Actually publish to eBay. Without this, the run is a dry run.")
    parser.add_argument("--no-email", action="store_true", help="Do not send the report email.")
    args = parser.parse_args()

    text = args.links if args.links is not None else sys.stdin.read()

    try:
        result = list_picked(text, live=args.live)
    except Exception as exc:  # noqa: BLE001 - top-level guard so we always email a failure
        result = {"date": _now().date().isoformat(), "niche": "picked", "status": "error",
                  "products": [], "listed_count": 0, "expected_count": 0,
                  "error": f"Unhandled error: {exc}"}

    try:
        import spend

        result["spend"] = spend.totals()
    except Exception:  # noqa: BLE001
        pass

    summary_dir = RUNS_DIR / str(result.get("run_stamp", "last-picked"))
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
    print("MODE:", "DRY RUN — nothing was listed" if not args.live else "LIVE — listings published")
    print(json.dumps({"status": result.get("status"),
                      "listed": result.get("listed_count", 0),
                      "expected": result.get("expected_count", 0),
                      "error": result.get("error", "")}))
    return 0 if result.get("status") in ("listed", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
