#!/usr/bin/env python3
"""Can this app fetch ONE product by id without a seller access token?

Why this exists. The daily run sources products without ``ALIEXPRESS_ACCESS_TOKEN``
because discovery (``aliexpress.ds.recommend.feed.get``, ali_api.discover) sends no
token — it asks "give me products out of this feed". The hand-picked-link paths
(list_picked.py / list_from_url.py) name a *specific* product, and the only DS method
that answers that, ``aliexpress.ds.product.get``, requires the token. DS apps cannot
keyword-search either, so there is no way around it inside the DS method set.

The candidate way out is the affiliate method ``aliexpress.affiliate.productdetail.get``,
which takes app key/secret + tracking ID and **no access token**. Whether this app is
granted it is a property of the AliExpress app registration, not of this code, so the
only way to find out is to make a real signed call. That is all this script does.

It is read-only: two ``.get`` methods, nothing created, purchased, or listed. It prints
method names, error codes and messages only — never a credential.

Run:
    python3 probe_ali_detail.py https://www.aliexpress.us/item/<id>.html
    python3 probe_ali_detail.py <product-id>

Exit 0 when the affiliate path looks viable, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import ali_api

# Verdicts, in the order the report can reach them.
VIABLE = "affiliate fallback viable"
NO_PERMISSION = "affiliate permission missing"
UNUSABLE = "affiliate reachable but response unusable"


def _rule(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 60 - len(title)))


def probe_ds_product_get(product_id: str) -> dict[str, Any]:
    """The token-gated path the links flow uses today. Attempted only with a token set,
    to tell 'token expired' apart from 'app never granted Dropshipping'."""
    _rule("aliexpress.ds.product.get (token-gated)")
    if not ali_api.access_token():
        print("skipped — ALIEXPRESS_ACCESS_TOKEN is not set")
        return {"attempted": False, "ok": False, "error": "no token"}
    try:
        detail = ali_api.get_product_detail(product_id)
    except ali_api.AliError as exc:
        # _call already surfaces AliExpress's real code / sub_msg.
        print(f"FAILED: {exc}")
        return {"attempted": True, "ok": False, "error": str(exc)}
    flat = ali_api.flatten_detail(detail)
    print("ok — the seller token works")
    print(f"  title    : {flat['title'][:70]}")
    print(f"  price    : {flat['price']}")
    print(f"  reviews  : {flat['reviews']}   rating: {flat['rating']}")
    print(f"  variants : {len(ali_api.parse_variants(detail))}")
    return {"attempted": True, "ok": True, "flat": flat}


def probe_affiliate_detail(product_id: str) -> dict[str, Any]:
    """The actual question: product-by-id with no access token."""
    _rule("aliexpress.affiliate.productdetail.get (token-free)")
    tracking_id = os.environ.get("ALIEXPRESS_TRACKING_ID", "").strip()
    print(f"tracking id set: {bool(tracking_id)}")
    try:
        payload = ali_api._call(
            "aliexpress.affiliate.productdetail.get",
            {
                "product_ids": product_id,
                "target_currency": ali_api.TARGET_CURRENCY,
                "target_language": ali_api.TARGET_LANGUAGE,
                "tracking_id": tracking_id,
                "country": ali_api.SHIP_TO_COUNTRY,
            },
        )
    except ali_api.AliError as exc:
        print(f"FAILED: {exc}")
        return {"ok": False, "error": str(exc)}
    print("ok — the call was accepted")
    return {"ok": True, "payload": payload}


def evaluate_affiliate_payload(payload: Any, product_id: str) -> dict[str, Any]:
    """A 200 proves nothing on its own. Check the response actually carries what the
    pipeline needs, by running it through the parsers already in ali_api."""
    _rule("would the response survive the existing pipeline?")
    products = ali_api._feed_products(payload)
    print(f"product objects found by _feed_products: {len(products)}")
    if not products:
        print("The response carries no product object in a shape _feed_products walks.")
        print(f"top-level keys: {sorted(payload)[:20] if isinstance(payload, dict) else type(payload)}")
        return {"usable": False, "missing": ["product object"]}

    card = products[0]
    flat = ali_api.flatten_card(card)
    print("flatten_card mapped it to:")
    print(f"  id      : {flat['id']}")
    print(f"  title   : {flat['title'][:70]}")
    print(f"  price   : {flat['price']}")
    print(f"  rating  : {round(flat['rating'], 2) if flat['rating'] is not None else None}")
    print(f"  orders  : {flat['orders']}")
    print(f"  reviews : {flat['reviews']}  (affiliate never provides this)")
    print(f"  images  : {len(flat['images'])}")
    print(f"  category: {flat.get('category')}")

    # These are what listing_job/normalize_source require to build a listing at all.
    missing = [name for name in ("id", "title", "price") if not flat.get(name)]
    if not flat["images"]:
        missing.append("images")
    if flat["id"] and flat["id"] != product_id:
        missing.append(f"id mismatch (asked {product_id}, got {flat['id']})")

    print(f"\ngate_reason: {ali_api.gate_reason(flat) or 'passes every gate'}")
    print("  (a gate miss is advisory on the picked-links path — not a blocker here)")
    if missing:
        print(f"\nMISSING what a listing needs: {', '.join(missing)}")
    return {"usable": not missing, "missing": missing, "flat": flat}


def main_with_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("product", help="AliExpress product URL or bare product id")
    args = parser.parse_args(argv)

    try:
        product_id = ali_api.product_id_from_url(args.product)
    except ali_api.AliError:
        product_id = "".join(ch for ch in args.product if ch.isdigit())
    if not product_id:
        print(f"Could not read a product id from {args.product!r}")
        return 1

    print(f"Probing product {product_id}")
    # Booleans only. The values are credentials and must never reach a log or artifact.
    print(f"ALIEXPRESS_APP_KEY set      : {bool(os.environ.get('ALIEXPRESS_APP_KEY', '').strip())}")
    print(f"ALIEXPRESS_APP_SECRET set   : {bool(os.environ.get('ALIEXPRESS_APP_SECRET', '').strip())}")
    print(f"ALIEXPRESS_TRACKING_ID set  : {bool(os.environ.get('ALIEXPRESS_TRACKING_ID', '').strip())}")
    print(f"ALIEXPRESS_ACCESS_TOKEN set : {bool(ali_api.access_token())}")

    # Each stage is independent: one failure must not cost us the rest of the diagnosis.
    ds = probe_ds_product_get(product_id)
    affiliate = probe_affiliate_detail(product_id)

    if affiliate["ok"]:
        evaluation = evaluate_affiliate_payload(affiliate["payload"], product_id)
        verdict = VIABLE if evaluation["usable"] else UNUSABLE
    else:
        evaluation = {"usable": False, "missing": []}
        verdict = NO_PERMISSION

    _rule("verdict")
    print(verdict.upper())
    if verdict == VIABLE:
        print("The links path can work without a seller token: fetch by id through the")
        print("affiliate method and route the product object through flatten_card.")
        print("Ceiling: affiliate carries no SKU/variant records and no freight, so")
        print("listings would be single-variation with estimated shipping — the same")
        print("fidelity the daily run already runs at. Variants still need the token.")
    elif verdict == NO_PERMISSION:
        print("This app cannot call the affiliate method, so there is no token-free way")
        print("to fetch a named product. Minting the seller token is the only path:")
        print("  python3 mint_ali_token.py --check     # is an existing token still good?")
        print("  python3 mint_ali_token.py --debug     # the real error, not a bare 400")
    else:
        print("The affiliate call was accepted but the response is missing what a")
        print(f"listing needs: {', '.join(evaluation['missing'])}")

    print("\n" + json.dumps({
        "product_id": product_id,
        "ds_product_get_ok": ds["ok"],
        "affiliate_ok": affiliate["ok"],
        "affiliate_usable": evaluation["usable"],
        "verdict": verdict,
    }))
    return 0 if verdict == VIABLE else 1


if __name__ == "__main__":
    raise SystemExit(main_with_args())
