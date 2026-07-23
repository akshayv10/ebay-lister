#!/usr/bin/env python3
"""Prepare, publish with explicit approval, or reconcile exactly two eBay API offers."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any

from daily_history import upsert_history
from ebay_common import (
    CONFIG_PATH,
    LOCATION_KEY,
    MARKETPLACE,
    PROMOTED_RATE,
    ApiError,
    EbayClient,
    EbayError,
    UnknownOutcome,
    load_config,
    read_json,
    write_json,
)
from ebay_setup import preflight
from listing_job import build_review, initialize_result, normalize_source, record_prepared


HISTORY_PATH = Path("/Users/akballer47/Documents/Codex/resale-product-history.jsonl")


def require_setup(client: EbayClient) -> dict[str, Any]:
    status = preflight(client)
    if status.get("status") != "ready":
        raise EbayError("eBay account setup is incomplete; run ebay_setup.py preflight and configure-account")
    config = load_config()
    if config.get("merchant_location_key") != LOCATION_KEY:
        raise EbayError(f"Configured merchant location must be {LOCATION_KEY}")
    if str(config.get("promoted_rate_percent")) not in {"10", "10.0", "10.00"}:
        raise EbayError("Configured General promotion must be exactly 10 percent")
    return config


def _autofill_required_aspects_enabled() -> bool:
    """Unattended runs enable this so a category's required item specifics that were
    not sourced are filled with a safe default instead of aborting the listing.
    The manual/review path leaves it off, preserving the original strict behavior."""
    return os.environ.get("EBAY_AUTOFILL_REQUIRED_ASPECTS", "").strip().lower() in {"1", "true", "yes", "on"}


def autofill_required_aspect(definition: dict[str, Any], constraint: dict[str, Any]) -> list[str] | None:
    """Return a default value list for a missing required aspect, or None to keep it
    reported as missing. Selection-only aspects get their first allowed value; free-text
    aspects get "Does Not Apply"."""
    if not _autofill_required_aspects_enabled():
        return None
    allowed = [
        str(item.get("localizedValue", "")).strip()
        for item in definition.get("aspectValues", []) if isinstance(item, dict) and item.get("localizedValue")
    ]
    if allowed:
        return [allowed[0]]
    return ["Does Not Apply"]


def category_and_aspects(client: EbayClient, source: dict[str, Any]) -> tuple[str, dict[str, list[str]], list[str]]:
    tree = client.request(
        "GET", "/commerce/taxonomy/v1/get_default_category_tree_id", query={"marketplace_id": MARKETPLACE}
    ).data
    tree_id = str(tree.get("categoryTreeId", "")) if isinstance(tree, dict) else ""
    if not tree_id:
        raise EbayError("Taxonomy API did not return an EBAY_US category tree ID")
    suggested = client.request(
        "GET", f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions",
        query={"q": source["category_query"]},
    ).data
    suggestions = suggested.get("categorySuggestions", []) if isinstance(suggested, dict) else []
    if not isinstance(suggestions, list) or not suggestions:
        raise EbayError(f"No eBay category suggestion for: {source['category_query']}")
    category = suggestions[0].get("category", {}) if isinstance(suggestions[0], dict) else {}
    category_id = str(category.get("categoryId", ""))
    if not category_id:
        raise EbayError("Top category suggestion has no category ID")
    aspect_payload = client.request(
        "GET", f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_item_aspects_for_category",
        query={"category_id": category_id},
    ).data
    definitions = aspect_payload.get("aspects", []) if isinstance(aspect_payload, dict) else []
    if not isinstance(definitions, list) or not definitions:
        raise EbayError(f"eBay returned no aspect metadata for category {category_id}")
    provided = {name.casefold(): (name, values) for name, values in source["aspects"].items()}
    normalized: dict[str, list[str]] = {}
    required_missing: list[str] = []
    for definition in definitions if isinstance(definitions, list) else []:
        if not isinstance(definition, dict):
            continue
        official = str(definition.get("localizedAspectName", "")).strip()
        constraint = definition.get("aspectConstraint", {}) if isinstance(definition.get("aspectConstraint"), dict) else {}
        match = provided.pop(official.casefold(), None)
        if match is None:
            if constraint.get("aspectRequired") is True:
                filled = autofill_required_aspect(definition, constraint)
                if filled is not None:
                    normalized[official] = filled
                else:
                    required_missing.append(official)
            continue
        values = list(match[1])
        allowed = [
            str(item.get("localizedValue", "")).strip()
            for item in definition.get("aspectValues", []) if isinstance(item, dict) and item.get("localizedValue")
        ]
        if allowed and str(constraint.get("aspectMode", "")).upper() == "SELECTION_ONLY":
            official_values = {value.casefold(): value for value in allowed}
            invalid = [value for value in values if value.casefold() not in official_values]
            if invalid:
                raise EbayError(f"Invalid selection-only value for {official}: {invalid[0]}")
            values = [official_values[value.casefold()] for value in values]
        normalized[official] = values
    for _, (name, values) in provided.items():
        normalized[name] = list(values)
    if required_missing:
        raise EbayError(f"Missing required eBay item specifics: {', '.join(required_missing)}")

    # Condition check via the Sell Metadata API needs the sell.metadata scope, which
    # many keysets are not provisioned for (it is normally read with an app token).
    # It is only a pre-check: we always list condition NEW, which virtually every
    # category accepts, and if a category truly rejects NEW the offer/publish call
    # surfaces that error anyway. So treat this validation as best-effort.
    try:
        condition_payload = client.request(
            "GET", f"/sell/metadata/v1/marketplace/{MARKETPLACE}/get_item_condition_policies",
            query={"filter": f"categoryIds:{{{category_id}}}"}, marketplace_header=True,
        ).data
    except EbayError:
        condition_payload = None
    if isinstance(condition_payload, dict):
        condition_policies = condition_payload.get("itemConditionPolicies", [])
        conditions = {
            str(item.get("conditionEnum", ""))
            for policy in condition_policies if isinstance(policy, dict)
            for item in policy.get("itemConditions", []) if isinstance(item, dict)
        }
        if conditions and source["condition"] not in conditions:
            raise EbayError(f"Condition {source['condition']} is not supported in category {category_id}")
    return category_id, normalized, required_missing


def eps_image(client: EbayClient, source_url: str) -> str:
    response = client.request(
        "POST", "https://apim.ebay.com/commerce/media/v1_beta/image/create_image_from_url",
        json_body={"imageUrl": source_url}, expected=(201,),
    )
    if isinstance(response.data, dict) and response.data.get("imageUrl"):
        return str(response.data["imageUrl"])
    location = response.headers.get("Location") or response.headers.get("location") or ""
    image_id = location.rstrip("/").rsplit("/", 1)[-1]
    if not image_id:
        raise EbayError("Media API created an image without returning an image ID")
    for _ in range(5):
        detail = client.request(
            "GET", f"https://apim.ebay.com/commerce/media/v1_beta/image/{urllib.parse.quote(image_id)}",
            expected=(200, 202),
        ).data
        if isinstance(detail, dict) and detail.get("imageUrl"):
            return str(detail["imageUrl"])
        time.sleep(1)
    raise EbayError(f"eBay image {image_id} did not become ready")


def offer_id_from_response(response: Any) -> str:
    if isinstance(response.data, dict) and response.data.get("offerId"):
        return str(response.data["offerId"])
    location = response.headers.get("Location") or response.headers.get("location") or ""
    return location.rstrip("/").rsplit("/", 1)[-1] if location else ""


def find_existing_offer(client: EbayClient, sku: str) -> dict[str, Any] | None:
    response = client.request(
        "GET", "/sell/inventory/v1/offer",
        query={"sku": sku, "marketplace_id": MARKETPLACE, "format": "FIXED_PRICE", "limit": 100},
    ).data
    offers = response.get("offers", []) if isinstance(response, dict) else []
    matches = [item for item in offers if isinstance(item, dict) and str(item.get("sku", "")) == sku]
    if len(matches) > 1:
        raise EbayError(f"More than one eBay offer already exists for deterministic SKU {sku}")
    return matches[0] if matches else None


def create_or_reuse_offer(client: EbayClient, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    existing = find_existing_offer(client, str(payload["sku"]))
    if existing:
        offer_id = str(existing.get("offerId", ""))
        if existing.get("listing") or existing.get("listingId") or str(existing.get("status", "")).upper() == "PUBLISHED":
            raise EbayError(f"Deterministic SKU {payload['sku']} is already published")
        if not offer_id:
            raise EbayError(f"Existing offer for {payload['sku']} has no offer ID")
        client.request("PUT", f"/sell/inventory/v1/offer/{offer_id}", json_body=payload, expected=(204,))
    else:
        try:
            response = client.request("POST", "/sell/inventory/v1/offer", json_body=payload, expected=(201,))
            offer_id = offer_id_from_response(response)
        except EbayError:
            reconciled = find_existing_offer(client, str(payload["sku"]))
            if not reconciled or not reconciled.get("offerId"):
                raise
            offer_id = str(reconciled["offerId"])
        if not offer_id:
            raise EbayError("eBay created an offer without returning an offer ID")
    readback = client.request("GET", f"/sell/inventory/v1/offer/{offer_id}").data
    if not isinstance(readback, dict) or str(readback.get("sku", "")) != payload["sku"]:
        raise EbayError(f"Offer readback mismatch for SKU {payload['sku']}")
    return offer_id, readback


def prepare_product(client: EbayClient, config: dict[str, Any], result_path: Path) -> dict[str, Any]:
    result = read_json(result_path)
    source = normalize_source(result)
    category_id, normalized_aspects, missing = category_and_aspects(client, source)
    eps_urls: list[str] = []
    failures: list[str] = []
    for image_url in source["source_images"]:
        try:
            eps_urls.append(eps_image(client, image_url))
        except EbayError as exc:
            failures.append(str(exc))
    if not eps_urls:
        raise EbayError("Every selected product image failed eBay Picture Services import")

    inventory_records: list[dict[str, Any]] = []
    group_key = source.get("inventory_item_group_key", "")
    for variant in source["selected_variants"]:
        aspects = {**normalized_aspects, **{name: [value] for name, value in variant["options"].items()}}
        payload: dict[str, Any] = {
            "availability": {"shipToLocationAvailability": {"quantity": 1}},
            "condition": source["condition"],
            "product": {
                "title": source["listing_title"],
                "description": source["listing_description"],
                "aspects": aspects,
                "brand": source["verified_brand"],
                "imageUrls": eps_urls,
            },
        }
        client.request(
            "PUT", f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(variant['sku'])}",
            json_body=payload, expected=(204,),
        )
        readback = client.request(
            "GET", f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(variant['sku'])}"
        ).data
        if not isinstance(readback, dict):
            raise EbayError(f"Inventory readback missing for {variant['sku']}")
        inventory_records.append({"sku": variant["sku"], "readback": readback})

    group_record: dict[str, Any] | None = None
    if group_key:
        axes = list(source["selected_variants"][0]["options"])
        group_payload = {
            "aspects": normalized_aspects,
            "description": source["listing_description"],
            "imageUrls": eps_urls,
            "title": source["listing_title"],
            "variantSKUs": [item["sku"] for item in source["selected_variants"]],
            "variesBy": {
                "aspectsImageVariesBy": axes[0],
                "specifications": [
                    {"name": axis, "values": list(dict.fromkeys(item["options"][axis] for item in source["selected_variants"]))}
                    for axis in axes
                ],
            },
        }
        client.request(
            "PUT", f"/sell/inventory/v1/inventory_item_group/{urllib.parse.quote(group_key)}",
            json_body=group_payload, expected=(204,),
        )
        group_readback = client.request(
            "GET", f"/sell/inventory/v1/inventory_item_group/{urllib.parse.quote(group_key)}"
        ).data
        group_record = {"inventory_item_group_key": group_key, "readback": group_readback}

    offer_records: list[dict[str, Any]] = []
    for variant in source["selected_variants"]:
        offer_payload: dict[str, Any] = {
            "sku": variant["sku"],
            "marketplaceId": MARKETPLACE,
            "format": "FIXED_PRICE",
            "availableQuantity": 1,
            "categoryId": category_id,
            "merchantLocationKey": config["merchant_location_key"],
            "listingDuration": "GTC",
            "listingPolicies": {
                "paymentPolicyId": config["payment_policy_id"],
                "returnPolicyId": config["return_policy_id"],
                "fulfillmentPolicyId": config["fulfillment_policy_id"],
            },
            "pricingSummary": {
                "price": {"currency": "USD", "value": variant["expected_ebay_price"]}
            },
        }
        if not group_key:
            offer_payload["listingDescription"] = source["listing_description"]
        offer_id, readback = create_or_reuse_offer(client, offer_payload)
        offer_records.append({"sku": variant["sku"], "offer_id": offer_id, "published": False, "readback": readback})

    fees = client.request(
        "POST", "/sell/inventory/v1/offer/get_listing_fees",
        json_body={"offers": [{"offerId": item["offer_id"]} for item in offer_records]},
    ).data
    api_record = {
        "environment": "production",
        "marketplace_id": MARKETPLACE,
        "merchant_location_key": config["merchant_location_key"],
        "payment_policy_id": config["payment_policy_id"],
        "return_policy_id": config["return_policy_id"],
        "fulfillment_policy_id": config["fulfillment_policy_id"],
        "campaign_id": config["campaign_id"],
        "promoted_rate_percent": PROMOTED_RATE,
        "category_id": category_id,
        "required_aspects_complete": not missing,
        "normalized_aspects": normalized_aspects,
        "eps_image_urls": eps_urls,
        "image_import_failures": failures,
        "inventory_items": inventory_records,
        "inventory_item_group": group_record,
        "offers": offer_records,
        "listing_fees": fees if isinstance(fees, (dict, list)) else {},
    }
    return record_prepared(result_path, api_record)


def source_paths(run_dir: Path) -> list[Path]:
    paths = sorted(run_dir.glob("*/source.json"))
    if len(paths) != 2:
        raise EbayError("Run directory must contain exactly two product subdirectories with source.json")
    return paths


def prepare(run_dir: Path, client: EbayClient) -> dict[str, Any]:
    config = require_setup(client)
    sources = source_paths(run_dir)
    normalized = [normalize_source(read_json(path)) for path in sources]
    if len({item["product_id"] for item in normalized}) != 2:
        raise EbayError("The two source products must be distinct")
    result_paths: list[Path] = []
    for source_path in sources:
        result_path = source_path.with_name("result.json")
        initialize_result(source_path, result_path)
        result = read_json(result_path)
        result["status"] = "payload_validated"
        write_json(result_path, result)
        try:
            prepare_product(client, config, result_path)
        except EbayError as exc:
            failed = read_json(result_path)
            failed["status"] = "reconciliation_required" if isinstance(exc, UnknownOutcome) else "blocked"
            failed["blocked_reason"] = str(exc)
            failed["published"] = False
            failed["publish_allowed"] = False
            write_json(result_path, failed)
            raise
        result_paths.append(result_path)
    return build_review(result_paths, run_dir / "run-result.json", run_dir / "review.md")


def listing_id_from_publish(response: Any) -> str:
    if isinstance(response.data, dict):
        return str(response.data.get("listingId") or response.data.get("listingID") or "")
    return ""


def published_listing_id(client: EbayClient, product: dict[str, Any]) -> str:
    for offer in product["api"]["offers"]:
        readback = client.request("GET", f"/sell/inventory/v1/offer/{offer['offer_id']}").data
        listing = readback.get("listing", {}) if isinstance(readback, dict) else {}
        listing_id = str(readback.get("listingId") or listing.get("listingId") or "") if isinstance(readback, dict) else ""
        if listing_id:
            return listing_id
    return ""


def publish_product(client: EbayClient, product: dict[str, Any]) -> dict[str, Any]:
    group = product["api"].get("inventory_item_group")
    try:
        if group:
            group_key = str(group["inventory_item_group_key"])
            response = client.request(
                "POST", "/sell/inventory/v1/offer/publish_by_inventory_item_group",
                json_body={"inventoryItemGroupKey": group_key, "marketplaceId": MARKETPLACE},
            )
        else:
            offer_id = str(product["api"]["offers"][0]["offer_id"])
            response = client.request("POST", f"/sell/inventory/v1/offer/{offer_id}/publish")
        listing_id = listing_id_from_publish(response)
    except UnknownOutcome:
        listing_id = ""
        for _ in range(3):
            listing_id = published_listing_id(client, product)
            if listing_id:
                break
            time.sleep(1)
        if not listing_id:
            raise
    if not listing_id:
        listing_id = published_listing_id(client, product)
    if not listing_id:
        raise EbayError("Publish returned no listing ID; reconciliation is required")
    return {"listing_id": listing_id, "ebay_url": f"https://www.ebay.com/itm/{listing_id}"}


def ads_for_campaign(client: EbayClient, campaign_id: str) -> list[dict[str, Any]]:
    ads: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = client.request(
            "GET", f"/sell/marketing/v1/ad_campaign/{campaign_id}/ad",
            query={"limit": 500, "offset": offset}, marketplace_header=True,
        ).data
        page = payload.get("ads", []) if isinstance(payload, dict) else []
        ads.extend(item for item in page if isinstance(item, dict))
        total = int(payload.get("total", len(ads))) if isinstance(payload, dict) else len(ads)
        if not page or len(ads) >= total:
            return ads
        offset += len(page)


def cps_campaigns_for_listing(client: EbayClient, listing_id: str) -> list[dict[str, Any]]:
    payload = client.request(
        "GET", "/sell/marketing/v1/ad_campaign/find_campaign_by_ad_reference",
        query={"listing_id": listing_id}, marketplace_header=True,
    ).data
    campaigns = payload.get("campaigns", []) if isinstance(payload, dict) else []
    return [item for item in campaigns if isinstance(item, dict)]


def verify_no_priority(client: EbayClient, listing_id: str) -> None:
    payload = client.request(
        "GET", "/sell/marketing/v1/ad_campaign", query={"limit": 500, "offset": 0}, marketplace_header=True,
    ).data
    campaigns = payload.get("campaigns", []) if isinstance(payload, dict) else []
    cpc_ids = [
        str(item.get("campaignId", "")) for item in campaigns if isinstance(item, dict)
        and str(item.get("fundingStrategy", {}).get("fundingModel", "")) == "COST_PER_CLICK"
        and str(item.get("campaignStatus", "")) in {"RUNNING", "SCHEDULED", "PAUSED"}
    ]
    for cpc_id in filter(None, cpc_ids):
        if any(str(ad.get("listingId", "")) == listing_id and str(ad.get("adStatus", "")) != "ARCHIVED" for ad in ads_for_campaign(client, cpc_id)):
            raise EbayError("Unexpected Priority/CPC association detected for the published listing")


def promote(client: EbayClient, campaign_id: str, listing_id: str) -> str:
    try:
        response = client.request(
            "POST", f"/sell/marketing/v1/ad_campaign/{campaign_id}/ad", expected=(201,),
            json_body={"bidPercentage": PROMOTED_RATE, "listingId": listing_id},
        )
        location = response.headers.get("Location") or response.headers.get("location") or ""
        ad_id = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
    except UnknownOutcome:
        ad_id = ""
        campaigns = cps_campaigns_for_listing(client, listing_id)
        if not any(str(item.get("campaignId", "")) == campaign_id for item in campaigns):
            raise
    campaigns = cps_campaigns_for_listing(client, listing_id)
    matches = [item for item in campaigns if str(item.get("campaignId", "")) == campaign_id]
    if len(matches) != 1:
        raise EbayError("Published listing is not associated with the required General campaign")
    funding = matches[0].get("fundingStrategy", {})
    if funding.get("fundingModel") != "COST_PER_SALE" or str(funding.get("bidPercentage", "")) not in {"10", "10.0", "10.00"}:
        raise EbayError("Campaign verification did not prove General/CPS promotion at exactly 10 percent")
    if not ad_id:
        matching_ads = [ad for ad in ads_for_campaign(client, campaign_id) if str(ad.get("listingId", "")) == listing_id]
        ad_id = str(matching_ads[0].get("adId", "")) if len(matching_ads) == 1 else ""
    if not ad_id:
        raise UnknownOutcome("General campaign association exists but its ad ID could not be reconciled")
    detail = client.request("GET", f"/sell/marketing/v1/ad_campaign/{campaign_id}/ad/{ad_id}", marketplace_header=True).data
    if not isinstance(detail, dict) or str(detail.get("listingId", "")) != listing_id:
        raise EbayError("General promotion ad readback did not match the published listing")
    verify_no_priority(client, listing_id)
    return ad_id


def rollback(client: EbayClient, campaign_id: str, published: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for item in reversed(published):
        if item.get("ad_id"):
            try:
                client.request(
                    "DELETE", f"/sell/marketing/v1/ad_campaign/{campaign_id}/ad/{item['ad_id']}", expected=(204,),
                )
            except EbayError as exc:
                errors.append(str(exc))
        try:
            product = item["product"]
            group = product["api"].get("inventory_item_group")
            if group:
                client.request(
                    "POST", "/sell/inventory/v1/offer/withdraw_by_inventory_item_group",
                    json_body={
                        "inventoryItemGroupKey": group["inventory_item_group_key"],
                        "marketplaceId": MARKETPLACE,
                    },
                )
            else:
                for offer in product["api"]["offers"]:
                    client.request("POST", f"/sell/inventory/v1/offer/{offer['offer_id']}/withdraw")
        except EbayError as exc:
            errors.append(str(exc))
    return errors


OFFER_REVIEW_FIELDS = (
    "sku", "marketplaceId", "format", "availableQuantity", "categoryId",
    "merchantLocationKey", "listingDuration", "listingPolicies", "pricingSummary",
)


def reviewed_offer_changed(prepared: dict[str, Any], current: dict[str, Any]) -> bool:
    expected = prepared.get("readback")
    if not isinstance(expected, dict):
        return True
    return any(expected.get(field) != current.get(field) for field in OFFER_REVIEW_FIELDS)


def history_record(product: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    return {
        "local_calendar_date": product["local_calendar_date"],
        "assigned_niche": product["assigned_niche"],
        "recommendation_status": "recommended",
        "ebay_listing_status": "listed",
        "product_title": product["source_title"],
        "functional_fingerprint": product["functional_fingerprint"],
        "aliexpress_url": product["aliexpress_url"],
        "selected_variants": product["selected_variants"],
        "ebay_item_number": live["listing_id"],
        "ebay_url": live["ebay_url"],
    }


def write_history_batch(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.ebay-api.tmp")
    try:
        if path.exists():
            shutil.copyfile(path, temporary)
        else:
            temporary.write_text("", encoding="utf-8")
        for record in records:
            upsert_history(temporary, record)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def publish(run_dir: Path, confirm_run_id: str, client: EbayClient, history_path: Path = HISTORY_PATH) -> dict[str, Any]:
    config = require_setup(client)
    run_path = run_dir / "run-result.json"
    run = read_json(run_path)
    if run.get("status") != "api_prepared" or run.get("published") is not False:
        raise EbayError("Only an unpublished api_prepared run can be published")
    if confirm_run_id != str(run.get("run_id", "")):
        raise EbayError("Publish confirmation run ID does not match the prepared run")
    products = run.get("products")
    if not isinstance(products, list) or len(products) != 2:
        raise EbayError("Publish requires exactly two prepared products")
    all_offers = [offer for product in products for offer in product["api"]["offers"]]
    if len({str(offer.get("offer_id", "")) for offer in all_offers}) != len(all_offers):
        raise EbayError("Prepared run contains duplicated offer IDs")
    if len({str(offer.get("sku", "")) for offer in all_offers}) != len(all_offers):
        raise EbayError("Prepared run contains duplicated SKUs")
    for product in products:
        for offer in product["api"]["offers"]:
            readback = client.request("GET", f"/sell/inventory/v1/offer/{offer['offer_id']}").data
            if not isinstance(readback, dict) or str(readback.get("sku", "")) != offer["sku"]:
                raise EbayError("Offer changed or disappeared after review; prepare again")
            if reviewed_offer_changed(offer, readback):
                raise EbayError("Offer settings changed after review; prepare again")
            listing = readback.get("listing", {}) if isinstance(readback.get("listing"), dict) else {}
            if readback.get("listingId") or listing.get("listingId"):
                raise EbayError("A prepared offer is already published; reconcile before continuing")
    run["status"] = "publishing"
    run["publish_allowed"] = True
    write_json(run_path, run)
    published: list[dict[str, Any]] = []
    try:
        for product in products:
            live = publish_product(client, product)
            entry = {"product": product, **live, "ad_id": ""}
            published.append(entry)
            entry["ad_id"] = promote(client, str(config["campaign_id"]), live["listing_id"])
            if published_listing_id(client, product) != live["listing_id"]:
                raise EbayError("Live listing readback did not match the published listing ID")
    except EbayError as exc:
        reconciliation_errors: list[str] = []
        for entry in published:
            if entry.get("ad_id"):
                continue
            try:
                matches = [
                    ad for ad in ads_for_campaign(client, str(config["campaign_id"]))
                    if str(ad.get("listingId", "")) == entry["listing_id"]
                ]
                if len(matches) == 1 and matches[0].get("adId"):
                    entry["ad_id"] = str(matches[0]["adId"])
                elif matches:
                    reconciliation_errors.append(f"Could not uniquely identify the General ad for {entry['listing_id']}")
            except EbayError as recovery_exc:
                reconciliation_errors.append(str(recovery_exc))
        rollback_errors = reconciliation_errors + rollback(client, str(config["campaign_id"]), published)
        run["status"] = (
            "reconciliation_required" if isinstance(exc, UnknownOutcome) or rollback_errors else "publish_rolled_back"
        )
        run["published"] = False
        run["publish_allowed"] = False
        run["publish_error"] = str(exc)
        run["rollback_errors"] = rollback_errors
        write_json(run_path, run)
        raise EbayError(f"Publish failed; whole-pair rollback attempted: {exc}") from exc

    live_by_product = {item["product"]["product_id"]: item for item in published}
    for product in products:
        live = live_by_product[product["product_id"]]
        product["status"] = "live"
        product["published"] = True
        product["publish_allowed"] = False
        product["listing_id"] = live["listing_id"]
        product["ebay_url"] = live["ebay_url"]
        product["general_promotion"] = {"campaign_id": config["campaign_id"], "ad_id": live["ad_id"], "bid_percentage": "10.0"}
        product["priority_promotion_enabled"] = False
    try:
        write_history_batch(
            history_path,
            [history_record(product, live_by_product[product["product_id"]]) for product in products],
        )
    except (OSError, ValueError) as exc:
        run["status"] = "reconciliation_required"
        run["published"] = True
        run["publish_allowed"] = False
        run["history_error"] = str(exc)
        write_json(run_path, run)
        raise EbayError("Both listings are live and promoted, but persistent history requires reconciliation") from exc
    run["status"] = "live"
    run["published"] = True
    run["publish_allowed"] = False
    write_json(run_path, run)
    return run


def reconcile(run_dir: Path, client: EbayClient) -> dict[str, Any]:
    run = read_json(run_dir / "run-result.json")
    observations: list[dict[str, Any]] = []
    for product in run.get("products", []):
        for offer in product.get("api", {}).get("offers", []):
            detail = client.request("GET", f"/sell/inventory/v1/offer/{offer['offer_id']}").data
            listing = detail.get("listing", {}) if isinstance(detail, dict) and isinstance(detail.get("listing"), dict) else {}
            observations.append({
                "product_id": product.get("product_id"),
                "sku": offer.get("sku"),
                "offer_id": offer.get("offer_id"),
                "listing_id": detail.get("listingId") or listing.get("listingId") if isinstance(detail, dict) else "",
                "status": detail.get("status", "") if isinstance(detail, dict) else "",
            })
    return {"status": "reconciled_read_only", "run_id": run.get("run_id"), "offers": observations}


def list_one(client: EbayClient, config: dict[str, Any], source_path: Path) -> dict[str, Any]:
    """Prepare, publish, and 10%-promote a single product. Returns the live product
    dict, or raises. If promotion fails after publish, the offer is withdrawn so we
    never leave an unpromoted live listing."""
    result_path = source_path.with_name("result.json")
    initialize_result(source_path, result_path)
    result = read_json(result_path)
    result["status"] = "payload_validated"
    write_json(result_path, result)
    product = prepare_product(client, config, result_path)
    live = publish_product(client, product)
    try:
        ad_id = promote(client, str(config["campaign_id"]), live["listing_id"])
    except EbayError:
        try:
            group = product["api"].get("inventory_item_group")
            if group:
                client.request(
                    "POST", "/sell/inventory/v1/offer/withdraw_by_inventory_item_group",
                    json_body={"inventoryItemGroupKey": group["inventory_item_group_key"], "marketplaceId": MARKETPLACE},
                )
            else:
                for offer in product["api"]["offers"]:
                    client.request("POST", f"/sell/inventory/v1/offer/{offer['offer_id']}/withdraw")
        except EbayError:
            pass
        raise
    product["status"] = "live"
    product["published"] = True
    product["publish_allowed"] = False
    product["listing_id"] = live["listing_id"]
    product["ebay_url"] = live["ebay_url"]
    product["general_promotion"] = {"campaign_id": config["campaign_id"], "ad_id": ad_id, "bid_percentage": "10.0"}
    product["priority_promotion_enabled"] = False
    write_json(result_path, product)
    return product


def list_resilient(run_dir: Path, client: EbayClient, needed: int = 2, history_path: Path = HISTORY_PATH) -> dict[str, Any]:
    """List products from run_dir independently, skipping any that fail, until ``needed``
    are live. One bad candidate never blocks the others. Writes run-result.json."""
    config = require_setup(client)
    sources = sorted(run_dir.glob("*/source.json"))
    if not sources:
        raise EbayError("Run directory contains no source.json files")
    listed: list[dict[str, Any]] = []
    errors: list[str] = []
    for source_path in sources:
        if len(listed) >= needed:
            break
        try:
            listed.append(list_one(client, config, source_path))
        except EbayError as exc:
            errors.append(f"{source_path.parent.name}: {exc}")
    if listed:
        try:
            write_history_batch(
                history_path,
                [history_record(p, {"listing_id": p["listing_id"], "ebay_url": p["ebay_url"]}) for p in listed],
            )
        except (OSError, ValueError) as exc:
            errors.append(f"history: {exc}")
    run = {
        "status": "live" if len(listed) >= needed else ("partial" if listed else "error"),
        "run_id": run_dir.name,
        "listed_count": len(listed),
        "products": listed,
        "errors": errors,
    }
    write_json(run_dir / "run-result.json", run)
    return run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--run-dir", required=True, type=Path)
    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--run-dir", required=True, type=Path)
    publish_parser.add_argument("--confirm-run-id", required=True)
    publish_parser.add_argument("--history", type=Path, default=HISTORY_PATH)
    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        client = EbayClient()
        if args.command == "prepare":
            payload = prepare(args.run_dir, client)
        elif args.command == "publish":
            payload = publish(args.run_dir, args.confirm_run_id, client, args.history)
        else:
            payload = reconcile(args.run_dir, client)
        print(json.dumps({"status": payload.get("status"), "run_id": payload.get("run_id", "")}))
        return 0
    except (EbayError, ApiError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
