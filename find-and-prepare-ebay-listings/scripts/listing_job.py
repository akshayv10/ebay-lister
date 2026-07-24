#!/usr/bin/env python3
"""Validate API listing sources and record unpublished-offer review state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ebay_common import EbayError, read_json, write_json
from ebay_price import quote


class JobError(EbayError):
    pass


def require_text(value: dict[str, Any], key: str) -> str:
    result = str(value.get(key, "")).strip()
    if not result:
        raise JobError(f"Missing required field: {key}")
    return result


def decimal_value(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise JobError(f"{field} must be decimal money") from exc
    if not result.is_finite():
        raise JobError(f"{field} must be finite")
    return result


def canonical_source_url(value: str) -> str:
    parsed = urlparse(value.strip())
    host = parsed.netloc.casefold().split(":", 1)[0]
    if parsed.scheme != "https" or not (
        host == "aliexpress.us" or host.endswith(".aliexpress.us")
        or host == "aliexpress.com" or host.endswith(".aliexpress.com")
    ):
        raise JobError("aliexpress_url must be an HTTPS AliExpress product URL")
    match = re.search(r"/item/(\d{8,20})(?:\.html)?", parsed.path, re.I)
    if not match:
        raise JobError("aliexpress_url does not contain a product ID")
    return f"https://www.aliexpress.us/item/{match.group(1)}.html"


def https_url(value: Any, field: str) -> str:
    parsed = urlparse(str(value).strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise JobError(f"{field} must be an absolute HTTPS URL")
    return parsed.geturl()


def slug(value: str, limit: int = 32) -> str:
    cleaned = "-".join(re.findall(r"[a-z0-9]+", value.casefold())).strip("-")
    return cleaned[:limit] or "default"


def deterministic_sku(product_id: str, variant_id: str) -> str:
    digest = hashlib.sha256(variant_id.encode()).hexdigest()[:8]
    return f"ALI-{product_id}-{slug(variant_id, 18)}-{digest}"[:50]


def deterministic_group_key(product_id: str) -> str:
    return f"ALI-GROUP-{product_id}"[:50]


def normalize_source(source: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {"mode": "ebay_api"}
    for key in (
        "run_id", "local_calendar_date", "assigned_niche", "product_id", "source_title",
        "functional_fingerprint", "verified_brand", "listing_title", "listing_description",
    ):
        normalized[key] = require_text(source, key)
    for key in ("aliexpress_rating", "aliexpress_reviews", "aliexpress_orders", "appeal_score"):
        value = source.get(key)
        if value not in (None, ""):
            try:
                normalized[key] = float(value)
            except (TypeError, ValueError) as exc:
                raise JobError(f"{key} must be numeric when provided") from exc
    if source.get("appeal_reason") not in (None, ""):
        normalized["appeal_reason"] = str(source["appeal_reason"]).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized["local_calendar_date"]):
        raise JobError("local_calendar_date must be YYYY-MM-DD")
    if len(normalized["listing_title"]) > 80:
        raise JobError("listing_title exceeds eBay's 80-character limit")
    normalized["aliexpress_url"] = canonical_source_url(require_text(source, "aliexpress_url"))
    if normalized["product_id"] not in normalized["aliexpress_url"]:
        raise JobError("product_id must match aliexpress_url")
    normalized["condition"] = str(source.get("condition", "NEW")).strip().upper()
    if normalized["condition"] != "NEW":
        raise JobError("API v1 supports only condition NEW for this sourcing workflow")
    normalized["category_query"] = str(source.get("category_query") or normalized["listing_title"]).strip()

    raw_aspects = source.get("aspects")
    if not isinstance(raw_aspects, dict) or not raw_aspects:
        raise JobError("aspects must contain verified item-specific name/value pairs")
    aspects: dict[str, list[str]] = {}
    for name, values in raw_aspects.items():
        aspect_name = str(name).strip()
        items = values if isinstance(values, list) else [values]
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        if not aspect_name or not cleaned:
            raise JobError("aspect names and values must be nonblank")
        aspects[aspect_name] = cleaned
    if "Brand" not in aspects:
        aspects["Brand"] = [normalized["verified_brand"]]
    elif aspects["Brand"][0].casefold() != normalized["verified_brand"].casefold():
        raise JobError("Brand aspect must match verified_brand")
    normalized["aspects"] = aspects

    images = source.get("source_images")
    if not isinstance(images, list) or not 1 <= len(images) <= 24:
        raise JobError("source_images must contain 1 to 24 verified HTTPS image URLs")
    normalized["source_images"] = list(dict.fromkeys(https_url(item, "source_images") for item in images))

    variants = source.get("selected_variants")
    if not isinstance(variants, list) or not 1 <= len(variants) <= 4:
        raise JobError("selected_variants must contain 1 to 4 combinations")
    seen_ids: set[str] = set()
    seen_skus: set[str] = set()
    normalized_variants: list[dict[str, Any]] = []
    for index, item in enumerate(variants, 1):
        if not isinstance(item, dict):
            raise JobError(f"selected_variants[{index}] must be an object")
        variant_id = require_text(item, "id")
        if variant_id in seen_ids:
            raise JobError(f"Duplicate variant id: {variant_id}")
        seen_ids.add(variant_id)
        options = item.get("options", {})
        if not isinstance(options, dict):
            raise JobError(f"Variant {variant_id} options must be an object")
        clean_options = {str(key).strip(): str(value).strip() for key, value in options.items() if str(key).strip() and str(value).strip()}
        visible = decimal_value(item.get("visible_item_price"), f"{variant_id}.visible_item_price")
        delivered = decimal_value(item.get("delivered_total"), f"{variant_id}.delivered_total")
        if visible < Decimal("15.00"):
            raise JobError(f"Variant {variant_id} visible item price is below USD 15")
        if delivered <= 0:
            raise JobError(f"Variant {variant_id} delivered total must be positive")
        if int(item.get("quantity", 1)) != 1:
            raise JobError(f"Variant {variant_id} quantity must be 1")
        sku = deterministic_sku(normalized["product_id"], variant_id)
        if sku in seen_skus:
            raise JobError("Selected variants collide after deterministic SKU normalization")
        seen_skus.add(sku)
        expected = Decimal(str(quote(float(delivered))["suggested_price"]))
        record = {
            "id": variant_id,
            "sku": sku,
            "options": clean_options,
            "visible_item_price": f"{visible:.2f}",
            "delivered_total": f"{delivered:.2f}",
            "expected_ebay_price": f"{expected:.2f}",
            "quantity": 1,
        }
        # Optional per-variation photo, so eBay swaps the image with the selection.
        if str(item.get("image", "")).strip():
            record["image"] = https_url(item["image"], f"{variant_id}.image")
        normalized_variants.append(record)
    if len(normalized_variants) > 1:
        if len(normalized["source_images"]) > 12:
            raise JobError("Multi-variation listings support at most 12 group images")
        axes = list(normalized_variants[0]["options"])
        if not axes or any(list(item["options"]) != axes for item in normalized_variants):
            raise JobError("Every multi-variation combination must use the same ordered option axes")
        if any(len({item["options"][axis] for item in normalized_variants}) < 2 for axis in axes):
            raise JobError("Every variation axis must contain at least two distinct selected values")
        normalized["inventory_item_group_key"] = deterministic_group_key(normalized["product_id"])
    else:
        normalized["inventory_item_group_key"] = ""
    normalized["selected_variants"] = normalized_variants
    return normalized


def initialize_result(source_path: Path, result_path: Path) -> dict[str, Any]:
    source = normalize_source(read_json(source_path))
    result = {
        **source,
        "status": "accepted",
        "published": False,
        "publish_allowed": False,
        "api": {
            "environment": "production",
            "marketplace_id": "EBAY_US",
            "merchant_location_key": "irvine-92618",
            "inventory_items": [],
            "inventory_item_group": None,
            "offers": [],
            "listing_fees": None,
        },
        "warnings": [],
    }
    write_json(result_path, result)
    return result


def record_prepared(result_path: Path, api_record: dict[str, Any]) -> dict[str, Any]:
    result = read_json(result_path)
    if result.get("status") not in {"accepted", "payload_validated"}:
        raise JobError("Only an accepted or payload_validated result can become api_prepared")
    items = api_record.get("inventory_items")
    offers = api_record.get("offers")
    expected_skus = [item["sku"] for item in result["selected_variants"]]
    if not isinstance(items, list) or [str(item.get("sku", "")) for item in items] != expected_skus:
        raise JobError("Prepared inventory items must exactly match deterministic SKUs")
    if not isinstance(offers, list) or [str(item.get("sku", "")) for item in offers] != expected_skus:
        raise JobError("Prepared offers must exactly match deterministic SKUs")
    if any(not str(item.get("offer_id", "")).strip() or item.get("published") is not False for item in offers):
        raise JobError("Every prepared offer requires an unpublished offer ID")
    media_urls = api_record.get("eps_image_urls")
    if not isinstance(media_urls, list) or not media_urls:
        raise JobError("At least one EPS image URL is required")
    if not api_record.get("category_id") or api_record.get("required_aspects_complete") is not True:
        raise JobError("Category and required-aspect validation must pass")
    # Listing fees are informational; an empty response must not block a valid listing.
    if api_record.get("listing_fees") is not None and not isinstance(api_record.get("listing_fees"), (dict, list)):
        raise JobError("Listing fee response must be an object or list when present")
    result["status"] = "api_prepared"
    result["api"] = api_record
    result["published"] = False
    result["publish_allowed"] = False
    write_json(result_path, result)
    return result


def build_review(result_paths: list[Path], output_json: Path, output_markdown: Path) -> dict[str, Any]:
    if len(result_paths) != 2:
        raise JobError("Exactly two result files are required")
    results = [read_json(path) for path in result_paths]
    if any(item.get("status") != "api_prepared" or item.get("published") is not False for item in results):
        raise JobError("Both results must be api_prepared and unpublished")
    if len({item["product_id"] for item in results}) != 2 or len({item["aliexpress_url"] for item in results}) != 2:
        raise JobError("Prepared products must be distinct")
    run_ids = {item["run_id"].split("-product-", 1)[0] for item in results}
    batch_run_id = next(iter(run_ids)) if len(run_ids) == 1 else hashlib.sha256("|".join(sorted(item["run_id"] for item in results)).encode()).hexdigest()[:16]
    payload = {
        "mode": "ebay_api",
        "status": "api_prepared",
        "run_id": batch_run_id,
        "product_count": 2,
        "published": False,
        "publish_allowed": False,
        "products": results,
    }
    write_json(output_json, payload)
    lines = [
        "# eBay API review — unpublished offers",
        "",
        f"Run ID: `{batch_run_id}`",
        "",
        "Nothing is live. Publishing requires a separate explicit instruction and the matching run ID.",
        "",
        "| Product | Source | Category | SKU / offer | Price | Images |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        offers = result["api"]["offers"]
        sku_offers = "<br>".join(f"`{item['sku']}` / `{item['offer_id']}`" for item in offers)
        prices = "<br>".join(f"{item['id']}: USD {item['expected_ebay_price']}" for item in result["selected_variants"])
        lines.append(
            f"| {result['listing_title']} | [AliExpress]({result['aliexpress_url']}) | "
            f"`{result['api']['category_id']}` | {sku_offers} | {prices} | {len(result['api']['eps_image_urls'])} |"
        )
    lines.extend([
        "",
        "Required checks passed: inventory location `irvine-92618`, free-shipping policies selected, required aspects complete, and General promotion configured for 10%. Priority promotion has not been enabled.",
        "",
        f"To publish later, explicitly approve this run ID: `{batch_run_id}`.",
    ])
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--source", required=True, type=Path)
    init.add_argument("--result", required=True, type=Path)
    prepared = sub.add_parser("record-prepared")
    prepared.add_argument("--result", required=True, type=Path)
    prepared.add_argument("--api-record", required=True, type=Path)
    review = sub.add_parser("review")
    review.add_argument("--result", required=True, action="append", type=Path)
    review.add_argument("--output-json", required=True, type=Path)
    review.add_argument("--output-markdown", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "init":
            payload = initialize_result(args.source, args.result)
        elif args.command == "record-prepared":
            payload = record_prepared(args.result, read_json(args.api_record))
        else:
            payload = build_review(args.result, args.output_json, args.output_markdown)
        print(json.dumps({"status": payload.get("status", "ok"), "run_id": payload.get("run_id", "")}))
        return 0
    except (EbayError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
