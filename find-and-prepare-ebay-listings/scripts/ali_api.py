#!/usr/bin/env python3
"""AliExpress Open Platform sourcing: search products, apply the daily-sourcing
eligibility gates, and emit per-product ``source.json`` payloads that
``listing_job.py`` / ``ebay_listing.py`` consume.

This replaces the browser-based sourcing described in references/daily-sourcing.md
with pure API calls so the daily job can run unattended (no browser, computer off).

Credentials come from the environment:
  ALIEXPRESS_APP_KEY, ALIEXPRESS_APP_SECRET, ALIEXPRESS_TRACKING_ID

Offline/testing: set ALI_API_FIXTURE to a JSON file containing a list of raw
product dicts (or {"products": [...]}) and no network call is made — used by
`daily_run.py --dry-run` and the unit tests.
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
TARGET_CURRENCY = os.environ.get("ALI_TARGET_CURRENCY", "USD")
TARGET_LANGUAGE = os.environ.get("ALI_TARGET_LANGUAGE", "EN")

# Eligibility thresholds (see references/daily-sourcing.md). Coarser than the
# browser page because the Affiliate API does not expose an exact star rating or
# review count; evaluate_rate (positive-feedback %) approximates the star rating
# (90% ~= 4.5 stars) and order volume stands in for the review-count gate.
MIN_EVALUATE_RATE_PCT = float(os.environ.get("ALI_MIN_EVALUATE_RATE_PCT", "90"))
MIN_ORDERS = int(os.environ.get("ALI_MIN_ORDERS", "100"))
MIN_PRICE_USD = Decimal(os.environ.get("ALI_MIN_PRICE_USD", "15"))
# Delivered-cost estimate for eBay pricing (AliExpress ships many items free to the
# US; the seller revises the price after posting). delivered = price*pct + flat.
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


class AliError(RuntimeError):
    pass


# --- Field extraction (defensive against Affiliate API response shapes) -----------

def _first_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        # Common TOP wrapping: {"string": [...]} or {"number": [...]}.
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
        for key in ("string", "value"):
            if key in value:
                return _string_list(value[key])
        return []
    if isinstance(value, str):
        # Occasionally a comma/`;`-joined string.
        parts = re.split(r"[;,]\s*", value.strip()) if value.strip() else []
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


def product_field(product: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in product and product[name] not in (None, ""):
            return product[name]
    return None


def extract_product_id(product: dict[str, Any]) -> str:
    raw = _first_str(product_field(product, "product_id", "productId", "item_id"))
    match = re.search(r"\d{8,20}", raw)
    return match.group(0) if match else ""


def extract_price_usd(product: dict[str, Any]) -> Decimal | None:
    raw = _first_str(product_field(
        product, "target_sale_price", "target_app_sale_price", "sale_price", "app_sale_price"
    ))
    raw = re.sub(r"[^0-9.]", "", raw)
    if not raw:
        return None
    try:
        return Decimal(raw)
    except Exception:  # noqa: BLE001 - malformed price is simply ineligible
        return None


def extract_evaluate_rate(product: dict[str, Any]) -> float:
    raw = _first_str(product_field(product, "evaluate_rate", "positive_feedback_rate", "avg_evaluation_rate"))
    match = re.search(r"[\d.]+", raw)
    return float(match.group(0)) if match else 0.0


def extract_orders(product: dict[str, Any]) -> int:
    raw = _first_str(product_field(product, "lastest_volume", "latest_volume", "orders", "sales"))
    match = re.search(r"\d+", raw.replace(",", ""))
    return int(match.group(0)) if match else 0


def extract_images(product: dict[str, Any]) -> list[str]:
    images: list[str] = []
    main = _first_str(product_field(product, "product_main_image_url", "image_url", "main_image"))
    if main:
        images.append(_https(main))
    for url in _string_list(product_field(product, "product_small_image_urls", "small_image_urls", "image_urls")):
        images.append(_https(url))
    seen: set[str] = set()
    unique: list[str] = []
    for url in images:
        if url.startswith("https://") and url not in seen:
            seen.add(url)
            unique.append(url)
    return unique[:MAX_IMAGES]


def extract_title(product: dict[str, Any]) -> str:
    return _first_str(product_field(product, "product_title", "title", "subject"))


def extract_category(product: dict[str, Any]) -> str:
    return _first_str(product_field(
        product, "second_level_category_name", "first_level_category_name", "category_name"
    ))


def detail_url(product: dict[str, Any], product_id: str) -> str:
    return f"https://www.aliexpress.us/item/{product_id}.html"


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
    lowered = title.casefold()
    return any(term in lowered for term in RESTRICTED_TERMS)


def gate_reason(product: dict[str, Any]) -> str | None:
    """Return None if the product passes all gates, else a short failure reason."""
    product_id = extract_product_id(product)
    if not product_id:
        return "no product id"
    title = extract_title(product)
    if not title:
        return "no title"
    if brand_excluded(title):
        return "excluded brand"
    if restricted(title):
        return "restricted category"
    if extract_evaluate_rate(product) < MIN_EVALUATE_RATE_PCT:
        return f"evaluate_rate < {MIN_EVALUATE_RATE_PCT}%"
    if extract_orders(product) < MIN_ORDERS:
        return f"orders < {MIN_ORDERS}"
    price = extract_price_usd(product)
    if price is None or price < MIN_PRICE_USD:
        return f"price < {MIN_PRICE_USD}"
    if not extract_images(product):
        return "no images"
    return None


# --- source.json construction -----------------------------------------------------

def delivered_total(price: Decimal) -> Decimal:
    return (price * (Decimal("1") + SHIP_PCT) + SHIP_FLAT).quantize(Decimal("0.01"))


def listing_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) <= 80:
        return title
    trimmed = title[:80]
    if " " in trimmed:
        trimmed = trimmed[:trimmed.rfind(" ")]
    return trimmed.strip() or title[:80]


def product_to_source(
    product: dict[str, Any],
    niche: str,
    run_stamp: str,
    local_date: str,
) -> dict[str, Any]:
    """Map a raw AliExpress product into a validated-shape source.json dict.
    Raises AliError if the product does not pass the gates."""
    reason = gate_reason(product)
    if reason is not None:
        raise AliError(f"Product ineligible: {reason}")
    product_id = extract_product_id(product)
    title = extract_title(product)
    price = extract_price_usd(product)
    assert price is not None  # guaranteed by gate_reason
    images = extract_images(product)
    category = extract_category(product) or "Accessories"
    ebay_title = listing_title(title)
    return {
        "run_id": f"{run_stamp}-product-{product_id}",
        "local_calendar_date": local_date,
        "assigned_niche": niche,
        "product_id": product_id,
        "aliexpress_url": detail_url(product, product_id),
        "source_title": title,
        "functional_fingerprint": normalized_identity(title),
        "verified_brand": "Unbranded",
        "listing_title": ebay_title,
        "listing_description": (
            f"{ebay_title}. Brand new, unused item in original packaging. "
            "Please review the photos and item specifics before purchase."
        ),
        "condition": "NEW",
        "category_query": category,
        "aspects": {"Brand": ["Unbranded"], "Type": [category]},
        "source_images": images,
        "selected_variants": [
            {
                "id": "default",
                "options": {},
                "visible_item_price": f"{price:.2f}",
                "delivered_total": f"{delivered_total(price):.2f}",
                "quantity": 1,
            }
        ],
    }


# --- AliExpress API transport (signed TOP gateway) --------------------------------

def _sign(params: dict[str, str], secret: str) -> str:
    """HMAC-SHA256 signature over sorted key+value concatenation (sign_method=sha256)."""
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
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - network/HTTP errors surface as AliError
        raise AliError(f"AliExpress request failed for {method}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AliError(f"AliExpress returned non-JSON for {method}: {raw[:300]}") from exc


def _products_from_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the nested Affiliate response to the product list, tolerant of shape."""
    node: Any = payload
    for _ in range(8):
        if isinstance(node, dict):
            if "products" in node:
                products = node["products"]
                if isinstance(products, dict):
                    products = products.get("product", products.get("products", []))
                if isinstance(products, list):
                    return [p for p in products if isinstance(p, dict)]
            # descend into the single nested dict value that looks like a wrapper
            next_node = None
            for key in ("resp_result", "result", "aliexpress_affiliate_product_query_response",
                        "aliexpress_affiliate_productdetail_get_response"):
                if key in node and isinstance(node[key], dict):
                    next_node = node[key]
                    break
            if next_node is None:
                return []
            node = next_node
        else:
            return []
    return []


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


def search_products(keywords: str, page_no: int = 1, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    fixture = _load_fixture()
    if fixture is not None:
        return fixture
    payload = _call(
        "aliexpress.affiliate.product.query",
        {
            "keywords": keywords,
            "page_no": str(page_no),
            "page_size": str(page_size),
            "target_currency": TARGET_CURRENCY,
            "target_language": TARGET_LANGUAGE,
            "ship_to_country": SHIP_TO_COUNTRY,
            "tracking_id": os.environ.get("ALIEXPRESS_TRACKING_ID", ""),
            "sort": "LAST_VOLUME_DESC",
        },
    )
    return _products_from_response(payload)


# --- High-level sourcing loop -----------------------------------------------------

def _duplicate(record_view: dict[str, Any], history: list[dict[str, Any]], accepted: list[dict[str, Any]]) -> bool:
    for existing in list(history) + accepted:
        if same_record(existing, record_view):
            return True
    return False


def source_products(
    niche: str,
    run_stamp: str,
    local_date: str,
    history: list[dict[str, Any]],
    needed: int = 2,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Search the niche's queries in order until ``needed`` distinct products
    qualify. Returns (sources, notes). ``notes`` records skipped candidates for
    the email report / debugging."""
    queries = NICHE_QUERIES.get(niche)
    if not queries:
        raise AliError(f"No search queries configured for niche: {niche}")
    accepted: list[dict[str, Any]] = []
    accepted_views: list[dict[str, Any]] = []
    accepted_ids: set[str] = set()
    notes: list[str] = []
    for query in queries:
        if len(accepted) >= needed:
            break
        for page in range(1, MAX_SEARCH_PAGES + 1):
            if len(accepted) >= needed:
                break
            try:
                products = search_products(query, page_no=page)
            except AliError as exc:
                notes.append(f"[{query} p{page}] {exc}")
                break
            if not products:
                break
            for product in products:
                if len(accepted) >= needed:
                    break
                product_id = extract_product_id(product)
                if not product_id or product_id in accepted_ids:
                    continue
                reason = gate_reason(product)
                if reason is not None:
                    continue
                view = {
                    "aliexpress_url": detail_url(product, product_id),
                    "functional_fingerprint": normalized_identity(extract_title(product)),
                    "product_title": extract_title(product),
                }
                if _duplicate(view, history, accepted_views):
                    continue
                try:
                    source = product_to_source(product, niche, run_stamp, local_date)
                except AliError as exc:
                    notes.append(f"[{product_id}] {exc}")
                    continue
                accepted.append(source)
                accepted_views.append(view)
                accepted_ids.add(product_id)
            if _load_fixture() is not None:
                break  # fixture returns a fixed set; do not paginate
        if _load_fixture() is not None:
            break
    return accepted, notes
