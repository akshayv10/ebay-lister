#!/usr/bin/env python3
"""Validate a two-product sourcing handoff and build exact-variant source files."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import ali_api
from ebay_common import write_json
from listing_job import canonical_source_url, normalize_source


SCHEMA_VERSIONS = {1, 2}
MAX_DISPATCH_INPUT = 65_535


class HandoffError(ValueError):
    pass


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise HandoffError(f"Missing required handoff field: {field}")
    return result


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise HandoffError(f"{field} must be numeric") from exc
    if result < 0:
        raise HandoffError(f"{field} must not be negative")
    return result


def _money(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HandoffError(f"{field} must be decimal money") from exc
    if not result.is_finite():
        raise HandoffError(f"{field} must be finite")
    return result


def _token(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _canonical_product(value: str) -> tuple[str, str]:
    url = canonical_source_url(value)
    match = re.search(r"/item/(\d{8,20})\.html$", url)
    if not match:
        raise HandoffError("AliExpress URL did not contain a canonical product ID")
    return match.group(1), url


def _variant_options(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise HandoffError(f"{field} must be an object")
    result: dict[str, str] = {}
    for name, option in value.items():
        clean_name = _text(name, f"{field}.name")
        clean_option = _text(option, f"{field}.{clean_name}")
        result[clean_name] = clean_option
    return result


def _batch_material(payload: dict[str, Any]) -> dict[str, Any]:
    products = sorted(
        (
            {
                "product_id": item["product_id"],
                "options": item["selected_variant"]["options"],
                "image_hashes": [
                    image["sha256"] for image in item.get("media", {}).get("images", [])
                ],
            }
            for item in payload["products"]
        ),
        key=lambda item: item["product_id"],
    )
    return {
        "local_calendar_date": payload["local_calendar_date"],
        "assigned_niche": payload["assigned_niche"],
        "products": products,
    }


def batch_id_for(payload: dict[str, Any]) -> str:
    material = json.dumps(_batch_material(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    date_part = payload["local_calendar_date"].replace("-", "")
    return f"frp-{date_part}-{digest}"


def _media(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HandoffError(f"{field} must be an object")
    images = value.get("images")
    if not isinstance(images, list) or not 1 <= len(images) <= 24:
        raise HandoffError(f"{field}.images must contain 1 to 24 EPS images")
    clean: list[dict[str, Any]] = []
    hashes: set[str] = set()
    orders: set[int] = set()
    for index, image in enumerate(images, 1):
        if not isinstance(image, dict):
            raise HandoffError(f"{field}.images[{index}] must be an object")
        digest = str(image.get("sha256") or "").strip().casefold()
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or digest in hashes:
            raise HandoffError(f"{field}.images[{index}].sha256 must be unique hexadecimal SHA-256")
        eps_url = _text(image.get("eps_url"), f"{field}.images[{index}].eps_url")
        if not eps_url.startswith("https://"):
            raise HandoffError(f"{field}.images[{index}].eps_url must use HTTPS")
        order = int(_number(image.get("order"), f"{field}.images[{index}].order"))
        if order < 1 or order in orders:
            raise HandoffError(f"{field}.images[{index}].order must be unique and positive")
        role = str(image.get("role") or "main").strip().casefold()
        if role not in {"main", "variant", "description"}:
            raise HandoffError(f"{field}.images[{index}].role is unsupported")
        options = _variant_options(
            image.get("variant_options", {}),
            f"{field}.images[{index}].variant_options",
        )
        hashes.add(digest)
        orders.add(order)
        clean.append(
            {
                "sha256": digest,
                "eps_image_id": _text(image.get("eps_image_id"), f"{field}.images[{index}].eps_image_id"),
                "eps_url": eps_url,
                "role": role,
                "order": order,
                "variant_options": options,
            }
        )
    clean.sort(key=lambda item: item["order"])
    return {
        "source": "alisave_all",
        "media_manifest_hash": _text(value.get("media_manifest_hash"), f"{field}.media_manifest_hash"),
        "downloaded_count": int(_number(value.get("downloaded_count", len(images)), f"{field}.downloaded_count")),
        "accepted_count": int(_number(value.get("accepted_count", len(images)), f"{field}.accepted_count")),
        "uploaded_count": int(_number(value.get("uploaded_count", len(images)), f"{field}.uploaded_count")),
        "attached_count": len(clean),
        "video_excluded_count": int(_number(value.get("video_excluded_count", 0), f"{field}.video_excluded_count")),
        "upload_failures": list(value.get("upload_failures", [])) if isinstance(value.get("upload_failures", []), list) else [],
        "images": clean,
    }


def validate_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HandoffError("Handoff payload must be a JSON object")
    schema_version = int(value.get("schema_version", 0))
    if schema_version not in SCHEMA_VERSIONS:
        raise HandoffError("schema_version must be 1 or 2")
    local_date = _text(value.get("local_calendar_date"), "local_calendar_date")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_date):
        raise HandoffError("local_calendar_date must be YYYY-MM-DD")
    niche = _text(value.get("assigned_niche"), "assigned_niche")
    raw_products = value.get("products")
    if not isinstance(raw_products, list) or len(raw_products) != 2:
        raise HandoffError("Handoff must contain exactly two products")

    products: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_products, 1):
        if not isinstance(raw, dict):
            raise HandoffError(f"products[{index}] must be an object")
        product_id, url = _canonical_product(_text(raw.get("aliexpress_url"), f"products[{index}].aliexpress_url"))
        supplied_id = _text(raw.get("product_id"), f"products[{index}].product_id")
        if supplied_id != product_id:
            raise HandoffError(f"products[{index}].product_id does not match its AliExpress URL")
        rating = _number(raw.get("rating"), f"products[{index}].rating")
        reviews = int(_number(raw.get("reviews"), f"products[{index}].reviews"))
        orders = int(_number(raw.get("orders"), f"products[{index}].orders"))
        if rating < ali_api.MIN_RATING or reviews < ali_api.MIN_REVIEWS or orders < ali_api.MIN_ORDERS:
            raise HandoffError(f"products[{index}] does not meet the sourcing thresholds")
        if raw.get("us_region_confirmed") is not True:
            raise HandoffError(f"products[{index}].us_region_confirmed must be true")
        selected = raw.get("selected_variant")
        if not isinstance(selected, dict):
            raise HandoffError(f"products[{index}].selected_variant must be an object")
        visible = _money(
            selected.get("visible_item_price"),
            f"products[{index}].selected_variant.visible_item_price",
        )
        if visible < ali_api.MIN_PRICE_USD:
            raise HandoffError(f"products[{index}] selected variant is below USD {ali_api.MIN_PRICE_USD}")
        checkout = selected.get("checkout_total")
        if checkout not in (None, "", "Unavailable"):
            checkout = f"{_money(checkout, f'products[{index}].selected_variant.checkout_total'):.2f}"
        else:
            checkout = ""
        normalized_product = {
                "product_id": product_id,
                "aliexpress_url": url,
                "product_title": _text(raw.get("product_title"), f"products[{index}].product_title"),
                "functional_fingerprint": _text(
                    raw.get("functional_fingerprint"),
                    f"products[{index}].functional_fingerprint",
                ),
                "verified_brand": _text(raw.get("verified_brand"), f"products[{index}].verified_brand"),
                "brand_evidence": _text(raw.get("brand_evidence"), f"products[{index}].brand_evidence"),
                "rating": rating,
                "reviews": reviews,
                "orders": orders,
                "us_region_confirmed": True,
                "selected_variant": {
                    "options": _variant_options(
                        selected.get("options", {}),
                        f"products[{index}].selected_variant.options",
                    ),
                    "display_label": _text(
                        selected.get("display_label"),
                        f"products[{index}].selected_variant.display_label",
                    ),
                    "visible_item_price": f"{visible:.2f}",
                    "checkout_total": checkout,
                },
                "material_risk": str(raw.get("material_risk") or "").strip(),
            }
        if schema_version == 2:
            normalized_product["media"] = _media(raw.get("media"), f"products[{index}].media")
        products.append(normalized_product)

    if len({item["product_id"] for item in products}) != 2:
        raise HandoffError("The two handoff products must be distinct")
    if len({_token(item["functional_fingerprint"]) for item in products}) != 2:
        raise HandoffError("The two handoff products must be functionally distinct")

    normalized = {
        "schema_version": schema_version,
        "local_calendar_date": local_date,
        "assigned_niche": niche,
        "products": products,
    }
    if schema_version == 2:
        normalized["publish_mode"] = "immediate"
    expected = batch_id_for(normalized)
    supplied_batch = str(value.get("batch_id") or "").strip()
    if supplied_batch and supplied_batch != expected:
        raise HandoffError(f"batch_id does not match the deterministic payload ID ({expected})")
    normalized["batch_id"] = expected
    return normalized


def decode_payload(encoded: str) -> dict[str, Any]:
    if not encoded:
        raise HandoffError("HANDOFF_PAYLOAD_B64 is not set")
    if len(encoded) > MAX_DISPATCH_INPUT:
        raise HandoffError(f"Encoded handoff exceeds {MAX_DISPATCH_INPUT} characters")
    try:
        raw = base64.b64decode(encoded, validate=True).decode("utf-8")
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffError("HANDOFF_PAYLOAD_B64 is not valid base64 JSON") from exc
    return validate_envelope(value)


def _options_match(wanted: dict[str, str], candidate: dict[str, str]) -> bool:
    wanted_map = {_token(name): _token(value) for name, value in wanted.items()}
    candidate_map = {_token(name): _token(value) for name, value in candidate.items()}
    if wanted_map and all(candidate_map.get(name) == value for name, value in wanted_map.items()):
        return True
    wanted_values = sorted(value for value in wanted_map.values() if value)
    candidate_values = sorted(value for value in candidate_map.values() if value)
    return bool(wanted_values and all(value in candidate_values for value in wanted_values))


def exact_variant(detail: dict[str, Any], selected: dict[str, Any]) -> dict[str, Any]:
    variants = ali_api.parse_variants(detail)
    wanted = selected["options"]
    if wanted:
        matches = [item for item in variants if _options_match(wanted, item.get("options", {}))]
    elif len(variants) == 1:
        matches = variants
    elif not variants:
        matches = []
    else:
        raise HandoffError("Exact variant is ambiguous; provide structured option names and values")

    if len(matches) > 1:
        browser_price = Decimal(selected["visible_item_price"])
        price_matches = [item for item in matches if item.get("price") == browser_price]
        if len(price_matches) == 1:
            matches = price_matches
    if len(matches) != 1:
        if not variants and not wanted:
            return {}
        raise HandoffError("Exact selected variant could not be resolved uniquely in current AliExpress data")
    return matches[0]


def source_from_product(product: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    detail = ali_api.get_product_detail(product["product_id"])
    flat = ali_api.flatten_detail(detail)
    if str(flat.get("id")) != product["product_id"]:
        raise HandoffError("AliExpress API product ID did not match the handoff")
    reason = ali_api.gate_reason(flat)
    # The DS detail flattener exposes the product's cheapest SKU. This handoff gates the
    # browser-selected exact SKU instead, so only the generic minimum-price reason may
    # be deferred until after exact variant resolution.
    if reason and not reason.startswith("price <"):
        raise HandoffError(f"Current AliExpress product failed a gate: {reason}")
    current = exact_variant(detail, product["selected_variant"])
    if current:
        current_price = Decimal(str(current["price"]))
        sku_id = str(current.get("sku_id") or "")
        image = str(current.get("image") or "")
    else:
        current_price = Decimal(str(flat["price"]))
        sku_id = str(flat.get("sku_id") or "")
        image = ""
    if current_price < ali_api.MIN_PRICE_USD:
        raise HandoffError(f"Current exact-variant price is below USD {ali_api.MIN_PRICE_USD}")

    shipping = ali_api.freight(product["product_id"], sku_id, current_price)
    delivered = ali_api.delivered_total(current_price, shipping)
    variant_id = f"sku-{sku_id}" if sku_id else "default"
    source = ali_api.product_to_source(
        flat,
        payload["assigned_niche"],
        payload["batch_id"],
        payload["local_calendar_date"],
        enforce_gates=False,
    )
    source["functional_fingerprint"] = product["functional_fingerprint"]
    source["verified_brand"] = product["verified_brand"]
    source["aspects"] = {"Brand": [product["verified_brand"]], "MPN": ["N/A"]}
    source["selected_variants"] = [
        {
            "id": variant_id,
            "options": product["selected_variant"]["options"],
            "visible_item_price": f"{current_price:.2f}",
            "delivered_total": f"{delivered:.2f}",
            "quantity": 1,
            **({"image": image} if image else {}),
        }
    ]
    source["handoff_evidence"] = {
        "browser_visible_item_price": product["selected_variant"]["visible_item_price"],
        "checkout_total": product["selected_variant"]["checkout_total"],
        "brand_evidence": product["brand_evidence"],
        "us_region_confirmed": True,
        "material_risk": product["material_risk"],
    }
    if payload["schema_version"] == 2:
        media = product["media"]
        # Multi-variation listings are capped at 12 images per variation. Keep the
        # ordered AliSave gallery; per-variation matching is applied during preparation.
        source["source_images"] = [item["eps_url"] for item in media["images"][:12]]
        source["media"] = media
        matching = [
            item["eps_url"] for item in media["images"]
            if item["variant_options"]
            and _options_match(product["selected_variant"]["options"], item["variant_options"])
        ]
        if matching:
            source["selected_variants"][0]["eps_images"] = matching[:12]
    normalize_source(source)
    return source


def blocked_result(product: dict[str, Any], payload: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "run_id": f"{payload['batch_id']}-product-{product['product_id']}",
        "local_calendar_date": payload["local_calendar_date"],
        "assigned_niche": payload["assigned_niche"],
        "product_id": product["product_id"],
        "aliexpress_url": product["aliexpress_url"],
        "source_title": product["product_title"],
        "functional_fingerprint": product["functional_fingerprint"],
        "verified_brand": product["verified_brand"],
        "selected_variant": product["selected_variant"],
        "status": "blocked",
        "blocked_reason": reason,
        "published": False,
        "publish_allowed": False,
    }


def build_run(payload: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "handoff.json", payload)
    results: list[dict[str, Any]] = []
    for index, product in enumerate(payload["products"], 1):
        product_dir = run_dir / f"product-{index}"
        product_dir.mkdir(parents=True, exist_ok=True)
        try:
            source = source_from_product(product, payload)
            write_json(product_dir / "source.json", source)
            results.append({"product_id": product["product_id"], "status": "source_validated"})
        except (ali_api.AliError, HandoffError, ValueError) as exc:
            result = blocked_result(product, payload, str(exc))
            write_json(product_dir / "result.json", result)
            results.append({"product_id": product["product_id"], "status": "blocked", "error": str(exc)})
    summary = {
        "status": "source_validated" if all(item["status"] == "source_validated" for item in results)
        else ("source_validated_partial" if any(item["status"] == "source_validated" for item in results) else "blocked"),
        "run_id": payload["batch_id"],
        "products": results,
    }
    write_json(run_dir / "handoff-build.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        payload = decode_payload(os.environ.get("HANDOFF_PAYLOAD_B64", ""))
        if args.run_dir.name != payload["batch_id"]:
            raise HandoffError("Run directory name must equal the deterministic batch ID")
        summary = build_run(payload, args.run_dir)
        print(json.dumps(summary))
        # A fully blocked pair is still a completed adapter result. The next stage writes
        # the review artifact and exits non-zero without touching eBay.
        return 0
    except (HandoffError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
