#!/usr/bin/env python3
"""Mint an AliExpress Dropshipping access token and print it for GitHub Secrets.

`aliexpress.ds.product.get` (variant data) and `ds.freight.calculate` require a seller
access_token; the feed methods do not. Run this once, then store the printed value as the
ALIEXPRESS_ACCESS_TOKEN repository secret.

NOTE: AliExpress states its refresh token does not work — when the access token expires,
re-run this script and update the secret. Until then the daily job simply falls back to
single-variant listings.

Set these first (same shell), then run:
    export ALIEXPRESS_APP_KEY='...'
    export ALIEXPRESS_APP_SECRET='...'
    python3 mint_ali_token.py
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request

# Override with ALIEXPRESS_AUTH_URL if the regional host differs
# (alternate: https://oauth.aliexpress.com/authorize).
AUTHORIZE_URL = os.environ.get("ALIEXPRESS_AUTH_URL", "https://api-sg.aliexpress.com/oauth/authorize")
TOKEN_URL = "https://api-sg.aliexpress.com/rest/auth/token/create"
DEFAULT_CALLBACK = "https://akshayv10.github.io/listing-oauth-pages/accepted.html"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"ERROR: {name} is not set. Export it first (see the header of this file).")
    return value


def _sign(params: dict[str, str], secret: str, path: str) -> str:
    """AliExpress /rest signing: path + sorted key/value pairs, HMAC-SHA256, uppercase hex."""
    concatenated = path + "".join(f"{key}{params[key]}" for key in sorted(params))
    return hmac.new(secret.encode(), concatenated.encode(), hashlib.sha256).hexdigest().upper()


def main() -> int:
    app_key = _require("ALIEXPRESS_APP_KEY")
    app_secret = _require("ALIEXPRESS_APP_SECRET")
    callback = os.environ.get("ALIEXPRESS_CALLBACK_URL", DEFAULT_CALLBACK).strip()

    # AliExpress docs are inconsistent: some use redirect_uri, others redirect_url.
    # Sending both is harmless and covers either expectation.
    consent = (
        f"{AUTHORIZE_URL}?"
        + urllib.parse.urlencode({
            "response_type": "code",
            "force_auth": "true",
            "redirect_uri": callback,
            "redirect_url": callback,
            "client_id": app_key,
        })
    )
    print("\nUsing:")
    print(f"  client_id (app key): {app_key}")
    print(f"  redirect_uri       : {callback}")
    print("  ^ this must EXACTLY match the Callback URL registered on your AliExpress app.")
    print("    Override with ALIEXPRESS_CALLBACK_URL if it differs.\n")
    print("1) Open this URL, sign in to AliExpress and approve:\n")
    print(consent)
    print("\n2) You will land on your callback page. Copy the COMPLETE URL from the")
    print("   address bar (it contains ?code=...), then paste it below.\n")

    pasted = getpass.getpass("Paste the complete callback URL (hidden): ").strip()
    if not pasted:
        return 1
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
    code = (query.get("code") or [""])[0].strip() or (pasted if "//" not in pasted else "")
    if not code:
        print("\nERROR: no ?code= found in that URL.")
        return 2

    path = "/auth/token/create"
    params = {
        "app_key": app_key,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "code": code,
    }
    params["sign"] = _sign(params, app_secret, path)
    request = urllib.request.Request(
        TOKEN_URL, data=urllib.parse.urlencode(params).encode(), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: token request failed: {exc}")
        return 2

    token = ""
    expires = ""
    stack = [payload]
    while stack:  # response nesting varies; find the token wherever it sits
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                low = key.lower()
                if low in {"access_token", "accesstoken"} and isinstance(value, str) and value:
                    token = value
                elif low in {"expire_time", "expires_in", "expire_in"} and not expires:
                    expires = str(value)
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)

    if not token:
        print("\nERROR: no access_token in the response:")
        print(json.dumps(payload)[:800])
        return 2

    print("\n" + "=" * 70)
    print("SUCCESS. Add this as the GitHub secret ALIEXPRESS_ACCESS_TOKEN:\n")
    print(token)
    if expires:
        print(f"\n(expires: {expires} — re-run this script and update the secret when it lapses)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
