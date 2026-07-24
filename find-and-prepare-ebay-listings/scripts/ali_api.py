#!/usr/bin/env python3
"""AliExpress **Dropshipping (DS) API** sourcing: discover products, apply the
daily-sourcing eligibility gates, and emit per-product ``source.json`` payloads
that ``listing_job.py`` / ``ebay_listing.py`` consume.

Replaces browser-based sourcing (references/daily-sourcing.md) with pure API calls
so the daily job runs unattended (no browser, computer off).

Two-step sourcing per candidate:
  1. Discovery -> candidate product IDs (aliexpress.ds.text.search, or the
     recommend-feed method). Env ALI_DS_DISCOVERY = auto | text | feed.
  2. Authoritative detail -> gates (aliexpress.ds.product.get), which returns a real
     1-5 star rating, review count, sales count, per-SKU price, and main images.
Delivered cost uses aliexpress.ds.freight.calculate when available, else an estimate.

Credentials (environment): ALIEXPRESS_APP_KEY, ALIEXPRESS_APP_SECRET, ALIEXPRESS_TRACKING_ID.

Offline/testing: set ALI_API_FIXTURE to a JSON file with a list of DS ``product.get``
result objects (each containing ``ae_item_base_info_dto``). No network call is made —
used by ``daily_run.py --dry-run`` and the unit tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Iterable

from daily_history import normalized_identity, same_record

# --- Configuration (overridable via environment) ---------------------------------

GATEWAY_URL = os.environ.get("ALIEXPRESS_GATEWAY", "https://api-sg.aliexpress.com/sync")
SHIP_TO_COUNTRY = os.environ.get("ALI_SHIP_TO_COUNTRY", "US")
SEND_FROM_COUNTRY = os.environ.get("ALI_SEND_FROM_COUNTRY", "CN")
TARGET_CURRENCY = os.environ.get("ALI_TARGET_CURRENCY", "USD")
TARGET_LANGUAGE = os.environ.get("ALI_TARGET_LANGUAGE", "EN")
DISCOVERY_MODE = os.environ.get("ALI_DS_DISCOVERY", "auto").strip().lower()  # auto|text|feed
# ds.freight.calculate also requires a seller access_token, so default it off; the
# delivered-cost estimate is used instead (the seller revises price after posting).
USE_FREIGHT = os.environ.get("ALI_USE_FREIGHT", "1").strip().lower() in {"1", "true", "yes", "on"}

# Eligibility thresholds (references/daily-sourcing.md). The DS product.get response
# provides a real star rating and review count, so these gates are exact (unlike the
# Affiliate API, which could only approximate them).
MIN_RATING = float(os.environ.get("ALI_MIN_RATING", "4.5"))
MIN_REVIEWS = int(os.environ.get("ALI_MIN_REVIEWS", "25"))
MIN_ORDERS = int(os.environ.get("ALI_MIN_ORDERS", "100"))
MIN_PRICE_USD = Decimal(os.environ.get("ALI_MIN_PRICE_USD", "15"))
# Delivered-cost estimate used only when freight lookup is unavailable/failed.
SHIP_PCT = Decimal(os.environ.get("ALI_SHIPPING_PCT", "0"))
SHIP_FLAT = Decimal(os.environ.get("ALI_SHIPPING_FLAT", "0"))

MAX_IMAGES = 24
MAX_SEARCH_PAGES = int(os.environ.get("ALI_MAX_SEARCH_PAGES", "6"))
PAGE_SIZE = int(os.environ.get("ALI_PAGE_SIZE", "40"))

# Niche -> rotating search queries. Broadened in order until two products qualify.
NICHE_QUERIES: dict[str, list[str]] = {
    "Smartphone Accessories": [
        "phone stand holder", "wireless charger stand", "phone camera lens kit",
        "car phone mount", "phone grip ring", "phone cooling fan",
    ],
    "Hobbyist & Interactive Toys": [
        "remote control car", "fidget toy set", "building blocks model",
        "magnetic drawing board", "interactive robot toy", "puzzle brain teaser",
    ],
    "Home Improvements & Lighting": [
        "led strip light", "motion sensor light", "smart light bulb",
        "under cabinet light", "solar garden light", "desk lamp dimmable",
    ],
    "Automotive Parts & Accessories": [
        "car organizer trunk", "car seat gap filler", "car trunk net",
        "car headrest hook", "car cup holder expander", "car door light",
    ],
    "Beauty & Self Care": [
        "facial cleansing brush", "makeup organizer", "hair styling tool",
        "gua sha roller", "led mirror vanity", "nail art kit",
    ],
}

# Niche -> DS feed names (from aliexpress.ds.feedname.get) to source from. Prefer
# US and price-banded feeds. Names must match feedname.get exactly (spaces included).
NICHE_FEEDS: dict[str, list[str]] = {
    "Smartphone Accessories": [
        "AEB_ PhoneAccessories_EG", "phones&accessories_ZA topsellers_ 20240423",
        "AEB_ ComputerAccessories_EG",
    ],
    "Hobbyist & Interactive Toys": [
        "DS_SHOPLAZZA_Toys&Hobbies_$20+_20241115", "US_Dolls&Accessories",
        "toys_ZA topsellers_ 20240423",
    ],
    "Home Improvements & Lighting": [
        "AEB_US_Lighting_TopSellers", "AEB_US_Home&Garden_TopSellers",
        "DS_Home&Kitchen_bestsellers", "light_ZA topsellers_ 20240423",
    ],
    "Automotive Parts & Accessories": [
        "DS_Automotive&Motorcycle 10$+", "AEB_Automobile&Accessories_bestsellers",
        "car&accessories_ZA topsellers_ 20240423",
    ],
    "Beauty & Self Care": [
        "USA_beauty&health_topsellers", "DS_Beauty_bestsellers",
    ],
}
# General fallback feeds if a niche's feeds return nothing usable.
FALLBACK_FEEDS = ["AEB_Droplo_BestsellersItems_20241016", "AEB_i69_FullCategory_TopSellers_20241225"]

# Spare parts / components / repair items. Free deterministic filter that runs before any
# AI call — these are what produced the "12pcs watercooling fittings" listing.
COMPONENT_TERMS = {
    "screw", "bolt", "washer", "ferrule", "fitting", "connector", "terminal",
    "flex cable", "ribbon cable", "pcb", "motherboard", "mainboard", "keycap",
    "spudger", "pry tool", "solder", "resistor", "capacitor", "transistor",
    "module board", "breakout board", "jumper wire", "dupont", "fpc",
    "digitizer", "lcd replacement", "screen replacement", "replacement screen",
    "battery replacement", "replacement battery", "sim tray", "thermal pad",
    "heat sink", "heatsink", "tubing", "bearing", "gasket", "o-ring", "bushing",
    "spare part", "replacement part", "repair kit", "repair tool", "grommet",
    "spare parts", "circuit board", "logic board", "flex ribbon",
}

# Established global brands to reject (illustrative, per daily-sourcing.md).
BRAND_EXCLUSIONS = {
    "apple", "iphone", "ipad", "airpods", "samsung", "galaxy", "lenovo", "sony",
    "google", "pixel", "microsoft", "xbox", "bose", "jbl", "beats", "nike",
    "adidas", "puma", "gopro", "dyson", "lego", "disney", "marvel", "nintendo",
    "playstation", "huawei", "xiaomi", "anker", "logitech", "philips", "gucci",
    "louis vuitton", "chanel", "rolex", "ferrari", "bmw", "mercedes", "toyota",
    "hp", "dell", "asus", "canon", "nikon", "fitbit", "garmin", "under armour",
}

# Reject restricted / risky product types.
RESTRICTED_TERMS = {
    "supplement", "vitamin", "pill", "capsule", "medicine", "medication", "drug",
    "cbd", "nicotine", "vape", "e-cigarette", "cigarette", "weight loss", "detox",
    "slimming", "pesticide", "taser", "pepper spray", "knife", "firearm", "ammo",
}

# Reject standalone electronic devices (as opposed to accessories) — they land in
# restricted eBay categories (e.g. Cell Phones 9355 rejecting condition NEW) and carry
# brand/authenticity risk. Substring match on the title.
DEVICE_EXCLUSIONS = {
    "smartphone", "cell phone", "cellphone", " gsm ", "rugged phone", "feature phone",
    "android phone", "mobile phone", "smart watch", "smartwatch", "tablet pc",
    "laptop computer", "game console", "drone with camera",
}


class AliError(RuntimeError):
    pass


# --- Small parsing helpers (tolerant of TOP response wrapping) --------------------

def _first_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("string", "number", "value"):
            if key in value:
                return _first_str(value[key])
    if isinstance(value, list) and value:
        return _first_str(value[0])
    return ""


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        # TOP wraps lists under various keys: {"string": [...]}, {"value": [...]},
        # and the DS feed's images under {"productSmallImageUrl": [...]}.
        for key in ("string", "value", "productSmallImageUrl", "productSmallImageUrls"):
            if key in value:
                return _string_list(value[key])
        for nested in value.values():  # fall back to the first list value present
            if isinstance(nested, list):
                return _string_list(nested)
        return []
    if isinstance(value, str):
        parts = re.split(r"[;\n,]\s*", value.strip()) if value.strip() else []
        return [p for p in parts if p]
    if isinstance(value, Iterable):
        out: list[str] = []
        for item in value:
            text = _first_str(item)
            if text:
                out.append(text)
        return out
    return []


def _https(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


def detail_url(product_id: str) -> str:
    return f"https://www.aliexpress.us/item/{product_id}.html"


# --- DS product.get parsing -> flat detail ----------------------------------------

def _find_result(payload: Any) -> dict[str, Any]:
    """Depth-first search for the dict that holds ``ae_item_base_info_dto``.
    Handles both the raw API envelope and a fixture that already is the result."""
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "ae_item_base_info_dto" in node:
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return {}


def _sku_list(result: dict[str, Any]) -> list[dict[str, Any]]:
    node = result.get("ae_item_sku_info_dtos")
    if isinstance(node, dict):
        for key in ("ae_item_sku_info_d_t_o", "ae_item_sku_info_dto"):
            if isinstance(node.get(key), list):
                return [s for s in node[key] if isinstance(s, dict)]
    if isinstance(node, list):
        return [s for s in node if isinstance(s, dict)]
    return []


def _decimal(raw: Any) -> Decimal | None:
    cleaned = re.sub(r"[^0-9.]", "", _first_str(raw))
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except Exception:  # noqa: BLE001
        return None


def _min_sku_price(result: dict[str, Any]) -> tuple[Decimal | None, str]:
    """Return the lowest single-unit price across SKUs and that SKU's id."""
    best: Decimal | None = None
    best_sku = ""
    for sku in _sku_list(result):
        price = None
        for key in ("offer_sale_price", "sku_price", "offer_price"):
            price = _decimal(sku.get(key))
            if price is not None:
                break
        if price is None:
            continue
        if best is None or price < best:
            best = price
            best_sku = _first_str(sku.get("sku_id") or sku.get("id") or sku.get("skuId"))
    return best, best_sku


def _images(result: dict[str, Any]) -> list[str]:
    media = result.get("ae_multimedia_info_dto")
    raw = media.get("image_urls") if isinstance(media, dict) else None
    urls = _string_list(raw)
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        https = _https(url)
        if https.startswith("https://") and https not in seen:
            seen.add(https)
            unique.append(https)
    return unique[:MAX_IMAGES]


def flatten_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Normalize a DS product.get result (or fixture) into a flat dict for gating."""
    result = _find_result(detail)
    base = result.get("ae_item_base_info_dto") if isinstance(result.get("ae_item_base_info_dto"), dict) else {}
    product_id = _first_str(base.get("product_id") or base.get("productId"))
    if not product_id:
        # fall back to any product id anywhere in the payload
        match = re.search(r"\d{8,20}", _first_str(base.get("subject")) or "")
        product_id = match.group(0) if match else _first_id_anywhere(detail)
    price, sku_id = _min_sku_price(result)
    rating_raw = _first_str(base.get("avg_evaluation_rating") or base.get("evaluation_rating"))
    return {
        "id": product_id,
        "title": _first_str(base.get("subject") or base.get("title")),
        "rating": float(re.search(r"[\d.]+", rating_raw).group(0)) if re.search(r"[\d.]+", rating_raw) else 0.0,
        "reviews": int(_first_str(base.get("evaluation_count") or base.get("review_count") or "0") or 0),
        "orders": int(re.sub(r"\D", "", _first_str(base.get("sales_count") or base.get("order_count"))) or 0),
        "price": price,
        "sku_id": sku_id,
        "images": _images(result),
    }


# Option axes that describe logistics rather than the product itself — never used as the
# eBay variation axis (a buyer shouldn't choose "Ships From").
LOGISTICS_AXES = {"ships from", "ship from", "shipping", "plug", "plug type", "voltage",
                  "warehouse", "country", "origin", "delivery"}
MAX_VARIANTS = int(os.environ.get("ALI_MAX_VARIANTS", "4"))


def _sku_property_names(result: dict[str, Any]) -> dict[str, str]:
    """Map SKU property id -> human name, from whichever property DTO the response uses."""
    names: dict[str, str] = {}
    stack: list[Any] = [result]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            pid = _first_str(node.get("sku_property_id") or node.get("attr_name_id") or node.get("property_id"))
            label = _first_str(node.get("sku_property_name") or node.get("attr_name") or node.get("property_name"))
            if pid and label:
                names.setdefault(pid, label)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return names


def parse_sku_attr(sku_attr: str, property_names: dict[str, str]) -> dict[str, str]:
    """'14:200004889#Black;5:361385#XL' -> {'Color': 'Black', 'Size': 'XL'}.

    Falls back to the property id as the axis name when the id isn't in the name map;
    the label is corrected downstream (openai_copy.normalize_variant_axis)."""
    options: dict[str, str] = {}
    for part in str(sku_attr or "").split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        prop_id, _, rest = part.partition(":")
        value = rest.split("#", 1)[1].strip() if "#" in rest else rest.strip()
        if not value:
            continue
        axis = property_names.get(prop_id.strip(), "").strip() or f"Option {prop_id.strip()}"
        options.setdefault(axis, value)
    return options


def parse_variants(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ds.product.get SKUs into [{sku_id, options, price, image, stock}]."""
    result = _find_result(detail)
    property_names = _sku_property_names(result)
    variants: list[dict[str, Any]] = []
    for sku in _sku_list(result):
        price = None
        for key in ("offer_sale_price", "sku_price", "offer_price"):
            price = _decimal(sku.get(key))
            if price is not None:
                break
        if price is None:
            continue
        options = parse_sku_attr(_first_str(sku.get("sku_attr") or sku.get("sku_attr_name")), property_names)
        stock_digits = re.sub(r"\D", "", _first_str(sku.get("sku_available_stock") or sku.get("ipm_sku_stock") or sku.get("sku_stock")))
        variants.append({
            "sku_id": _first_str(sku.get("sku_id") or sku.get("id") or sku.get("skuId")),
            "options": options,
            "price": price,
            "image": _https(_first_str(sku.get("sku_image") or sku.get("sku_image_url"))),
            "stock": int(stock_digits) if stock_digits else 0,
        })
    return variants


def choose_variant_axis(variants: list[dict[str, Any]]) -> str:
    """Pick the single most useful option axis, ignoring logistics axes."""
    counts: dict[str, set[str]] = {}
    for variant in variants:
        for axis, value in variant["options"].items():
            if axis.strip().casefold() in LOGISTICS_AXES:
                continue
            counts.setdefault(axis, set()).add(value)
    usable = {axis: values for axis, values in counts.items() if len(values) >= 2}
    if not usable:
        return ""
    return max(usable, key=lambda axis: len(usable[axis]))


def select_variants(variants: list[dict[str, Any]], limit: int = MAX_VARIANTS) -> tuple[str, list[dict[str, Any]]]:
    """Return (axis, chosen variants) — one in-stock, >= MIN_PRICE_USD variant per axis value."""
    axis = choose_variant_axis(variants)
    if not axis:
        return "", []
    best: dict[str, dict[str, Any]] = {}
    for variant in variants:
        value = variant["options"].get(axis, "").strip()
        if not value or variant["price"] is None or variant["price"] < MIN_PRICE_USD:
            continue
        current = best.get(value)
        # Prefer in-stock, then the cheaper option for a given axis value.
        if current is None or (variant["stock"] > 0 and current["stock"] <= 0) or (
            (variant["stock"] > 0) == (current["stock"] > 0) and variant["price"] < current["price"]
        ):
            best[value] = variant
    chosen = sorted(best.values(), key=lambda v: (v["stock"] <= 0, v["price"]))[:limit]
    if len(chosen) < 2:
        return "", []
    return axis, chosen


def variant_records(product_id: str) -> tuple[str, list[dict[str, Any]]]:
    """Fetch a product's SKUs and return (axis, source.json-shaped variant records).

    Each record carries its own AliExpress checkout price (SKU price + per-SKU freight) and
    its own image, so eBay shows the right price and photo per variation. Returns ("", [])
    when there is no usable axis; raises AliError without an access token.
    """
    detail = get_product_detail(product_id)
    axis, chosen = select_variants(parse_variants(detail))
    if not axis:
        return "", []
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for variant in chosen:
        value = variant["options"][axis]
        variant_id = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or variant["sku_id"]
        while variant_id in seen_ids:  # ids must be unique within the listing
            variant_id = f"{variant_id}-{variant['sku_id'][-4:] or len(seen_ids)}"
        seen_ids.add(variant_id)
        price = variant["price"]
        shipping = freight(product_id, variant["sku_id"], price)
        records.append({
            "id": variant_id,
            "options": {axis: value},
            "visible_item_price": f"{price:.2f}",
            "delivered_total": f"{delivered_total(price, shipping):.2f}",
            "quantity": 1,
            "image": variant["image"],
        })
    return axis, records


def _first_id_anywhere(payload: Any) -> str:
    for pid in _candidate_ids(payload):
        return pid
    return ""


def flatten_card(card: dict[str, Any]) -> dict[str, Any]:
    """Flatten a candidate into gate-ready fields. A DS product.get result (or fixture)
    is delegated to flatten_detail; a recommend-feed product object is parsed directly
    from its own fields so no per-product detail call is needed."""
    if "ae_item_base_info_dto" in card or _find_result(card):
        return flatten_detail(card)
    pid_match = re.search(r"\d{8,20}", _first_str(
        card.get("product_id") or card.get("productId") or card.get("item_id")))
    price = _decimal(
        card.get("target_sale_price") or card.get("target_app_sale_price")
        or card.get("sale_price") or card.get("app_sale_price"))
    rate_match = re.search(r"[\d.]+", _first_str(card.get("evaluate_rate") or card.get("positive_feedback_rate")))
    rating = float(rate_match.group(0)) / 20.0 if rate_match else None  # percent -> 5-star; None if absent
    orders_digits = re.sub(r"\D", "", _first_str(
        card.get("lastest_volume") or card.get("latest_volume") or card.get("orders")))
    orders = int(orders_digits) if orders_digits else None
    images: list[str] = []
    main = _https(_first_str(card.get("product_main_image_url") or card.get("image_url")))
    if main:
        images.append(main)
    for url in _string_list(card.get("product_small_image_urls") or card.get("image_urls")):
        images.append(_https(url))
    seen: set[str] = set()
    unique = [u for u in images if u.startswith("https://") and not (u in seen or seen.add(u))]
    return {
        "id": pid_match.group(0) if pid_match else "",
        "title": _first_str(card.get("product_title") or card.get("title") or card.get("subject")),
        "rating": rating,
        "reviews": None,  # not provided by the feed
        "orders": orders,
        "price": price,
        "sku_id": "",
        "images": unique[:MAX_IMAGES],
    }


def _feed_products(payload: Any) -> list[dict[str, Any]]:
    """Extract product objects from a recommend.feed.get response. The list is under
    result.products.<something> (e.g. traffic_product_d_t_o), so take the first list
    value found inside any 'products' object."""
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("products"), dict):
                for value in node["products"].values():
                    if isinstance(value, list):
                        return [p for p in value if isinstance(p, dict)]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return []


# --- Eligibility gates ------------------------------------------------------------

def brand_excluded(title: str) -> bool:
    identity = normalized_identity(title)
    words = set(identity.split())
    for brand in BRAND_EXCLUSIONS:
        if " " in brand:
            if brand in identity:
                return True
        elif brand in words:
            return True
    return False


def restricted(title: str) -> bool:
    lowered = f" {title.casefold()} "
    return any(term in lowered for term in RESTRICTED_TERMS | DEVICE_EXCLUSIONS)


def is_component(title: str) -> bool:
    """True for spare parts / repair components that make dull eBay listings."""
    lowered = f" {title.casefold()} "
    return any(term in lowered for term in COMPONENT_TERMS)


def gate_reason(flat: dict[str, Any]) -> str | None:
    """None if the flat detail passes every gate, else a short failure reason."""
    if not flat.get("id"):
        return "no product id"
    title = flat.get("title", "")
    if not title:
        return "no title"
    if brand_excluded(title):
        return "excluded brand"
    if restricted(title):
        return "restricted category"
    if is_component(title):
        return "spare part / component"
    # rating/reviews/orders are enforced only when the source provides them (feed cards
    # may omit some; the feeds are curated topsellers/bestsellers, so absence is OK).
    rating = flat.get("rating")
    if rating is not None and rating < MIN_RATING:
        return f"rating < {MIN_RATING}"
    reviews = flat.get("reviews")
    if reviews is not None and reviews < MIN_REVIEWS:
        return f"reviews < {MIN_REVIEWS}"
    orders = flat.get("orders")
    if orders is not None and orders < MIN_ORDERS:
        return f"orders < {MIN_ORDERS}"
    price = flat.get("price")
    if price is None or price < MIN_PRICE_USD:
        return f"price < {MIN_PRICE_USD}"
    if not flat.get("images"):
        return "no images"
    return None


# --- source.json construction -----------------------------------------------------

def delivered_total(price: Decimal, shipping: Decimal | None) -> Decimal:
    if shipping is not None:
        return (price + shipping).quantize(Decimal("0.01"))
    return (price * (Decimal("1") + SHIP_PCT) + SHIP_FLAT).quantize(Decimal("0.01"))


def listing_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) <= 80:
        return title
    trimmed = title[:80]
    if " " in trimmed:
        trimmed = trimmed[:trimmed.rfind(" ")]
    return trimmed.strip() or title[:80]


def product_to_source(flat: dict[str, Any], niche: str, run_stamp: str, local_date: str) -> dict[str, Any]:
    """Map a flattened DS detail into a validated-shape source.json dict.
    Raises AliError if the product does not pass the gates."""
    reason = gate_reason(flat)
    if reason is not None:
        raise AliError(f"Product ineligible: {reason}")
    product_id = flat["id"]
    title = flat["title"]
    price = flat["price"]
    shipping = freight(product_id, flat.get("sku_id", ""), price)
    ebay_title = listing_title(title)
    return {
        "run_id": f"{run_stamp}-product-{product_id}",
        "local_calendar_date": local_date,
        "assigned_niche": niche,
        "product_id": product_id,
        "aliexpress_rating": flat.get("rating"),
        "aliexpress_reviews": flat.get("reviews"),
        "aliexpress_orders": flat.get("orders"),
        "aliexpress_url": detail_url(product_id),
        "source_title": title,
        "functional_fingerprint": normalized_identity(title),
        "verified_brand": "Unbranded",
        "listing_title": ebay_title,
        "listing_description": (
            f"{ebay_title}. Brand new, unused item in original packaging. "
            "Please review the photos and item specifics before purchase."
        ),
        "condition": "NEW",
        # eBay taxonomy resolves well from the title; category-required item specifics
        # are auto-filled downstream (EBAY_AUTOFILL_REQUIRED_ASPECTS), so we send only
        # the verified Brand to avoid selection-only aspect conflicts.
        "category_query": ebay_title,
        # eBay's BrandMPN rule requires an MPN whenever Brand is supplied. MPN is free
        # text so it cannot trip selection-only validation. (AI enrichment may add more
        # item specifics at listing time; this is the fallback set.)
        "aspects": {"Brand": ["Unbranded"], "MPN": ["N/A"]},
        "source_images": flat["images"],
        "selected_variants": [
            {
                "id": "default",
                "options": {},
                "visible_item_price": f"{price:.2f}",
                "delivered_total": f"{delivered_total(price, shipping):.2f}",
                "quantity": 1,
            }
        ],
    }


# --- AliExpress TOP gateway transport (signed) ------------------------------------

def _sign(params: dict[str, str], secret: str) -> str:
    """HMAC-SHA256 over sorted key+value concatenation (sign_method=sha256)."""
    concatenated = "".join(f"{key}{params[key]}" for key in sorted(params))
    return hmac.new(secret.encode("utf-8"), concatenated.encode("utf-8"), hashlib.sha256).hexdigest().upper()


def _call(method: str, business_params: dict[str, str]) -> dict[str, Any]:
    app_key = os.environ.get("ALIEXPRESS_APP_KEY", "").strip()
    app_secret = os.environ.get("ALIEXPRESS_APP_SECRET", "").strip()
    if not app_key or not app_secret:
        raise AliError("ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET are not set")
    params: dict[str, str] = {
        "method": method,
        "app_key": app_key,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "format": "json",
        "v": "2.0",
    }
    params.update({k: v for k, v in business_params.items() if v not in (None, "")})
    params["sign"] = _sign(params, app_secret)
    url = f"{GATEWAY_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=30) as response:
            raw = response.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise AliError(f"AliExpress request failed for {method}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AliError(f"AliExpress returned non-JSON for {method}: {raw[:300]}") from exc
    if os.environ.get("ALI_DEBUG", "").strip():
        # Response bodies from these product APIs contain no secrets (the app key/secret
        # are only in the request). Print a snippet so we can see the real structure.
        snippet = json.dumps(data)[:4000] if isinstance(data, (dict, list)) else str(data)[:4000]
        print(f"[ALI_DEBUG] {method} -> {snippet}", flush=True)
    # AliExpress reports API-level failures as HTTP 200 with an error_response body.
    if isinstance(data, dict) and "error_response" in data:
        err = data.get("error_response") or {}
        raise AliError(
            f"{method} error {err.get('code', '?')}: "
            f"{err.get('msg', '')} {err.get('sub_code', '')} {err.get('sub_msg', '')}".strip()
        )
    return data


def _candidate_ids(payload: Any) -> list[str]:
    """Collect product-id-like values anywhere in a discovery response."""
    ids: list[str] = []
    seen: set[str] = set()
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in {"product_id", "productid", "item_id", "itemid"}:
                    match = re.search(r"\d{8,20}", _first_str(value))
                    if match and match.group(0) not in seen:
                        seen.add(match.group(0))
                        ids.append(match.group(0))
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return ids


# --- DS API methods ---------------------------------------------------------------

def access_token() -> str:
    return os.environ.get("ALIEXPRESS_ACCESS_TOKEN", "").strip()


def get_product_detail(product_id: str) -> dict[str, Any]:
    """Full product detail including SKUs/variants. Requires a seller access_token."""
    token = access_token()
    if not token:
        raise AliError("ALIEXPRESS_ACCESS_TOKEN is not set (needed for variant data)")
    payload = _call(
        "aliexpress.ds.product.get",
        {
            "product_id": product_id,
            "ship_to_country": SHIP_TO_COUNTRY,
            "target_currency": TARGET_CURRENCY,
            "target_language": TARGET_LANGUAGE,
            "access_token": token,
        },
    )
    return _find_result(payload)


def freight(product_id: str, sku_id: str, price: Decimal) -> Decimal | None:
    """Best-effort real US shipping cost. Returns None (caller uses the estimate)
    on any error, in fixture mode, or when disabled."""
    token = access_token()
    if not USE_FREIGHT or _load_fixture() is not None or not token:
        return None
    if not os.environ.get("ALIEXPRESS_APP_KEY", "").strip():
        return None
    try:
        payload = _call(
            "aliexpress.ds.freight.calculate",
            {
                "product_id": product_id,
                "sku_id": sku_id,
                "product_num": "1",
                "country_code": SHIP_TO_COUNTRY,
                "send_goods_country_code": SEND_FROM_COUNTRY,
                "price": f"{price:.2f}",
                "price_currency": TARGET_CURRENCY,
                "access_token": token,
            },
        )
    except AliError:
        return None
    costs: list[Decimal] = []
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in {"freight_amount", "amount", "shipping_fee", "fee"}:
                    amount = _decimal(value.get("amount") if isinstance(value, dict) else value)
                    if amount is not None:
                        costs.append(amount)
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return min(costs) if costs else None


_FEED_NAMES_CACHE: list[str] | None = None


def feed_names() -> list[str]:
    """The DS feeds available to this app (aliexpress.ds.feedname.get). Cached.
    An ALI_DS_FEED_NAME env value overrides discovery of the list."""
    global _FEED_NAMES_CACHE
    if _FEED_NAMES_CACHE is not None:
        return _FEED_NAMES_CACHE
    override = os.environ.get("ALI_DS_FEED_NAME", "").strip()
    if override:
        _FEED_NAMES_CACHE = [override]
        return _FEED_NAMES_CACHE
    names: list[str] = []
    try:
        payload = _call("aliexpress.ds.feedname.get", {})
    except AliError:
        payload = {}
    seen: set[str] = set()
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in {"promo_name", "feed_name", "name"} and isinstance(value, str) and value.strip():
                    if value.strip() not in seen:
                        seen.add(value.strip())
                        names.append(value.strip())
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    _FEED_NAMES_CACHE = names
    return names


def niche_feeds(niche: str) -> list[str]:
    """Feeds to source from for the day's niche. Sourcing (and therefore selection) is
    scoped to the niche chosen by the 5-niche daily rotation (daily_history.choose_niche):
    we page this niche's bestseller/topseller feeds and pick the top sellers within it.
    Falls back to the general bestseller feeds only when the app exposes none of the
    niche's feeds."""
    available = set(feed_names())
    wanted = NICHE_FEEDS.get(niche, [])
    chosen = [f for f in wanted if not available or f in available]
    if not chosen:
        chosen = [f for f in FALLBACK_FEEDS if not available or f in available]
    return chosen or wanted or FALLBACK_FEEDS


def discover(niche: str, page: int) -> list[dict[str, Any]]:
    """Return feed product objects for this niche's feeds (or fixture detail dicts).
    DS apps cannot keyword-search, so we page through niche-matched product feeds."""
    fixture = _load_fixture()
    if fixture is not None:
        return fixture
    feeds = niche_feeds(niche)
    feed_name = feeds[(page - 1) % len(feeds)]
    payload = _call(
        "aliexpress.ds.recommend.feed.get",
        {
            "feed_name": feed_name,
            "page_no": str(((page - 1) // len(feeds)) + 1),
            "page_size": str(PAGE_SIZE),
            "target_currency": TARGET_CURRENCY,
            "target_language": TARGET_LANGUAGE,
            "country": SHIP_TO_COUNTRY,
        },
    )
    return _feed_products(payload)


def _card_id(card: dict[str, Any]) -> str:
    if "__product_id__" in card:
        return str(card["__product_id__"])
    return flatten_detail(card).get("id", "")


def _card_is_detail(card: dict[str, Any]) -> bool:
    return "ae_item_base_info_dto" in card or bool(_find_result(card))


# --- Fixture (offline) ------------------------------------------------------------

def _load_fixture() -> list[dict[str, Any]] | None:
    path = os.environ.get("ALI_API_FIXTURE", "").strip()
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = data.get("products", [])
    if not isinstance(data, list):
        raise AliError("ALI_API_FIXTURE must be a JSON list or {'products': [...]}")
    return [p for p in data if isinstance(p, dict)]


# --- High-level sourcing loop -----------------------------------------------------

def _duplicate(view: dict[str, Any], history: list[dict[str, Any]], accepted: list[dict[str, Any]]) -> bool:
    return any(same_record(existing, view) for existing in list(history) + accepted)


def source_products(
    niche: str,
    run_stamp: str,
    local_date: str,
    history: list[dict[str, Any]],
    needed: int = 2,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build a pool of gate-passing candidates across the consumer feeds, rank the pool by
    resale appeal (AI), and return the best ``needed`` as source records.
    Falls back to deterministic order when the ranker is unavailable.
    Returns (sources, notes)."""
    in_fixture = _load_fixture() is not None
    pool_size = int(os.environ.get("ALI_POOL_SIZE", "40"))
    accepted: list[dict[str, Any]] = []
    accepted_views: list[dict[str, Any]] = []
    accepted_ids: set[str] = set()
    notes: list[str] = []
    failed_gates = 0
    seen = 0

    # Gather a pool first — appeal ranking needs choices, not the first two hits.
    target = needed if in_fixture else max(needed, pool_size)

    def enough() -> bool:
        return len(accepted) >= target

    feeds = ["fixture"] if in_fixture else niche_feeds(niche)
    max_pages = 1 if in_fixture else min(24, len(feeds) * 4)
    consecutive_empty = 0
    for page in range(1, max_pages + 1):
        if enough():
            break
        try:
            cards = discover(niche, page)
        except AliError as exc:
            notes.append(f"[p{page}] {exc}")
            break
        if not cards:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue
        consecutive_empty = 0
        for card in cards:
            if enough():
                break
            seen += 1
            try:
                # Gate on the feed's own fields (no product.get: it needs a seller token).
                flat = flatten_card(card)
                product_id = flat.get("id", "")
                if not product_id or product_id in accepted_ids:
                    continue
                if gate_reason(flat) is not None:
                    failed_gates += 1
                    continue
                view = {
                    "aliexpress_url": detail_url(product_id),
                    "functional_fingerprint": normalized_identity(flat["title"]),
                    "product_title": flat["title"],
                }
                if _duplicate(view, history, accepted_views):
                    continue
                source = product_to_source(flat, niche, run_stamp, local_date)
            except AliError as exc:
                notes.append(f"[card] {exc}")
                continue
            # Raw bestseller signals for deterministic ranking (rank_pool). Kept off
            # source.json — stripped before the record is returned.
            source["_signals"] = {
                "orders": flat.get("orders") or 0,
                "rating": flat.get("rating") or 0.0,
                "reviews": flat.get("reviews") or 0,
                "price": float(flat["price"]) if flat.get("price") is not None else 0.0,
            }
            accepted.append(source)
            accepted_views.append(view)
            accepted_ids.add(product_id)
        if in_fixture:
            break

    notes.append(
        f"pool: seen={seen} failed_gates={failed_gates} candidates={len(accepted)} "
        f"feeds={len(feeds)}"
    )
    selected = rank_pool(accepted, needed, notes)
    if len(selected) < needed:
        notes.append(f"selected {len(selected)} of {needed} requested")
    return selected, notes


def bestseller_key(source: dict[str, Any]) -> tuple[float, float, float, float]:
    """Sort key for "bestseller within the niche": highest sales volume (orders) wins,
    with rating, review count, then price as tie-breakers only. Missing signals count as
    0, so an item with no rating/review data can never outrank a genuine high-volume
    seller. Use with reverse=True (all fields ranked descending)."""
    signals = source.get("_signals") or {}
    return (
        float(signals.get("orders") or 0),
        float(signals.get("rating") or 0.0),
        float(signals.get("reviews") or 0),
        float(signals.get("price") or 0.0),
    )


def _strip_internal(source: dict[str, Any]) -> dict[str, Any]:
    """Remove ranking-only scratch keys so the returned record is a clean source.json."""
    source.pop("_signals", None)
    return source


def rank_deterministic(pool: list[dict[str, Any]], needed: int, notes: list[str]) -> list[dict[str, Any]]:
    """Pick the top ``needed`` bestsellers (by sales volume) from the gated pool, skipping
    any candidate that is not functionally distinct from one already chosen. No AI."""
    ordered = sorted(pool, key=bestseller_key, reverse=True)
    picked: list[dict[str, Any]] = []
    picked_views: list[dict[str, Any]] = []
    for source in ordered:
        if len(picked) >= needed:
            break
        view = {
            "aliexpress_url": source.get("aliexpress_url", ""),
            "functional_fingerprint": source.get("functional_fingerprint", ""),
            "product_title": source.get("source_title", ""),
        }
        if any(same_record(existing, view) for existing in picked_views):
            continue
        orders = int((source.get("_signals") or {}).get("orders") or 0)
        source["appeal_score"] = float(orders)
        source["appeal_reason"] = f"top seller in niche ({orders} orders)"
        notes.append(f"pick {orders} orders — {source.get('source_title','')[:60]}")
        picked.append(source)
        picked_views.append(view)
    return [_strip_internal(s) for s in picked]


def rank_pool(pool: list[dict[str, Any]], needed: int, notes: list[str]) -> list[dict[str, Any]]:
    """Select the best ``needed`` products from the gated pool. Deterministic by default
    (top bestsellers by sales volume, functionally distinct). Set ALI_AI_RANK=1 to opt
    into AI-scored resale-appeal ranking instead; any AI failure falls back to the
    deterministic ranker."""
    if len(pool) <= 1:
        return [_strip_internal(s) for s in pool[:needed]]
    ai_rank = os.environ.get("ALI_AI_RANK", "").strip().lower() in {"1", "true", "yes", "on"}
    if not ai_rank:
        return rank_deterministic(pool, needed, notes)
    try:
        import openai_copy
        import spend

        by_id = {source["product_id"]: source for source in pool}
        candidates = [
            {
                "id": source["product_id"],
                "title": source.get("source_title", ""),
                "price": (source.get("selected_variants") or [{}])[0].get("visible_item_price", ""),
            }
            for source in pool
        ]
        result = openai_copy.rank_candidates(candidates, top_n=needed)
        tokens_in, tokens_out = result.get("usage", (0, 0))
        spend.record(result.get("model", ""), tokens_in, tokens_out, purpose="ranking")
        ordered: list[dict[str, Any]] = []
        for entry in result.get("ranked", []):
            source = by_id.get(entry["id"])
            if source is None:
                continue
            source["appeal_score"] = entry["score"]
            source["appeal_reason"] = entry["reason"]
            notes.append(f"pick {entry['score']:.0f}/10 — {source.get('source_title','')[:60]} — {entry['reason'][:80]}")
            ordered.append(source)
        if ordered:
            return [_strip_internal(s) for s in ordered[:needed]]
        notes.append("AI ranker returned no usable picks; using deterministic order")
    except Exception as exc:  # noqa: BLE001 - AI ranking is best-effort
        notes.append(f"AI ranking unavailable ({exc}); using deterministic order")
    return rank_deterministic(pool, needed, notes)
