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
        for key in ("string", "value"):
            if key in value:
                return _string_list(value[key])
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
    rating = float(rate_match.group(0)) / 20.0 if rate_match else 0.0  # percent -> 5-star
    orders = int(re.sub(r"\D", "", _first_str(
        card.get("lastest_volume") or card.get("latest_volume") or card.get("orders"))) or 0)
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
    """Extract the product objects from a recommend.feed.get response."""
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "products" in node:
                products = node["products"]
                if isinstance(products, dict):
                    products = products.get("product") or products.get("products") or []
                if isinstance(products, list):
                    return [p for p in products if isinstance(p, dict)]
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
    lowered = title.casefold()
    return any(term in lowered for term in RESTRICTED_TERMS)


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
    if flat.get("rating", 0.0) < MIN_RATING:
        return f"rating < {MIN_RATING}"
    # Feed cards do not carry a review count (reviews is None); orders is the proxy.
    reviews = flat.get("reviews")
    if reviews is not None and reviews < MIN_REVIEWS:
        return f"reviews < {MIN_REVIEWS}"
    if flat.get("orders", 0) < MIN_ORDERS:
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
        "aspects": {"Brand": ["Unbranded"]},
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
        snippet = json.dumps(data)[:1500] if isinstance(data, (dict, list)) else str(data)[:1500]
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

def get_product_detail(product_id: str) -> dict[str, Any]:
    payload = _call(
        "aliexpress.ds.product.get",
        {
            "product_id": product_id,
            "ship_to_country": SHIP_TO_COUNTRY,
            "target_currency": TARGET_CURRENCY,
            "target_language": TARGET_LANGUAGE,
        },
    )
    return _find_result(payload)


def freight(product_id: str, sku_id: str, price: Decimal) -> Decimal | None:
    """Best-effort real US shipping cost. Returns None (caller uses the estimate)
    on any error, in fixture mode, or when disabled."""
    if not USE_FREIGHT or _load_fixture() is not None or not os.environ.get("ALIEXPRESS_APP_KEY", "").strip():
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


def discover(query: str, page: int) -> list[dict[str, Any]]:
    """Return candidate detail dicts (fixture) or lean cards with product ids.
    DS apps cannot keyword-search (text.search is unavailable), so we page through
    the app's product feeds; niche relevance is filtered later by the product title."""
    fixture = _load_fixture()
    if fixture is not None:
        return fixture
    names = feed_names()
    if not names:
        raise AliError("no DS feeds available (aliexpress.ds.feedname.get returned none)")
    # Round-robin a feed per page so successive pages explore different feeds.
    feed_name = names[(page - 1) % len(names)]
    payload = _call(
        "aliexpress.ds.recommend.feed.get",
        {
            "feed_name": feed_name,
            "page_no": str(page),
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


def _niche_tokens(niche: str) -> set[str]:
    tokens: set[str] = set()
    for phrase in NICHE_QUERIES.get(niche, []):
        for token in re.findall(r"[a-z]+", phrase.lower()):
            if len(token) >= 4:
                tokens.add(token)
    return tokens


def source_products(
    niche: str,
    run_stamp: str,
    local_date: str,
    history: list[dict[str, Any]],
    needed: int = 2,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Page through the app's DS feeds until ``needed`` distinct products qualify.
    Feeds are not keyword-targeted, so each candidate must also match the day's niche
    by title (unless ALI_NICHE_FILTER=0). Returns (sources, notes)."""
    in_fixture = _load_fixture() is not None
    niche_filter = os.environ.get("ALI_NICHE_FILTER", "1").strip().lower() in {"1", "true", "yes", "on"}
    niche_tokens = _niche_tokens(niche)
    accepted: list[dict[str, Any]] = []
    accepted_views: list[dict[str, Any]] = []
    accepted_ids: set[str] = set()
    notes: list[str] = []
    off_niche = 0
    filtered_gate = 0

    def enough() -> bool:
        return len(accepted) >= needed

    max_pages = 1 if in_fixture else min(15, max(MAX_SEARCH_PAGES, len(feed_names()) * 2))
    consecutive_empty = 0
    for page in range(1, max_pages + 1):
        if enough():
            break
        try:
            cards = discover("", page)
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
            try:
                # flatten_card parses the feed object directly (no per-product network call)
                flat = flatten_card(card)
                product_id = flat.get("id", "")
                if not product_id or product_id in accepted_ids:
                    continue
                if niche_filter and niche_tokens and not in_fixture:
                    title_l = flat.get("title", "").casefold()
                    if not any(token in title_l for token in niche_tokens):
                        off_niche += 1
                        continue
                if gate_reason(flat) is not None:
                    filtered_gate += 1
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
            accepted.append(source)
            accepted_views.append(view)
            accepted_ids.add(product_id)
        if in_fixture:
            break

    if len(accepted) < needed:
        notes.append(
            f"feeds={feed_names() if not in_fixture else 'fixture'} "
            f"off_niche_skipped={off_niche} failed_gates={filtered_gate} accepted={len(accepted)}"
        )
    return accepted, notes
