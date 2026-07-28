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

import argparse
import getpass
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

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


def _sign(params: dict[str, str], secret: str, path: str, debug: bool = False) -> str:
    """AliExpress /rest signing: path + sorted key/value pairs, HMAC-SHA256, uppercase hex."""
    concatenated = path + "".join(f"{key}{params[key]}" for key in sorted(params))
    if debug:
        # A signature mismatch is otherwise invisible — AliExpress just says "invalid
        # signature" without telling you what it expected. Print exactly what we signed.
        print("\n[debug] signing base string (app secret NOT shown):")
        print(f"[debug]   {concatenated}")
    return hmac.new(secret.encode(), concatenated.encode(), hashlib.sha256).hexdigest().upper()


def _post(url: str, params: dict[str, str], debug: bool = False) -> dict[str, Any]:
    """POST and always surface AliExpress's own error body.

    urllib raises HTTPError for 4xx/5xx, and the useful part — error_code,
    error_message, the request id — is in the response *body*, which str(exc) does not
    include. Reading it is the difference between "HTTP Error 400: Bad Request" and an
    actionable message.
    """
    request = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"\nERROR: token request failed with HTTP {exc.code}.")
        print("AliExpress said:")
        print(f"  {body[:1500] or '(empty response body)'}")
        _explain(body)
        raise SystemExit(2) from exc
    except urllib.error.URLError as exc:
        print(f"\nERROR: could not reach {url}: {exc.reason}")
        raise SystemExit(2) from exc
    if debug:
        print(f"\n[debug] raw response: {raw[:1500]}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"\nERROR: non-JSON response from {url}:\n  {raw[:800]}")
        raise SystemExit(2) from None


# AliExpress error codes seen on this endpoint, mapped to the thing to actually change.
_HINTS = (
    ("IncompleteSignature", "The signature is wrong. Re-run with --debug and check the base string."),
    ("invalid_signature", "The signature is wrong. Re-run with --debug and check the base string."),
    ("InvalidTimestamp", "Your clock is off. Sync system time and retry."),
    ("expired", "The authorization code expired — they are single-use and short-lived. Redo the consent step and paste a fresh URL immediately."),
    ("code is invalid", "That code was already used or has expired. Redo the consent step for a fresh one."),
    ("redirect", "The callback URL does not exactly match the one registered on your AliExpress app. Set ALIEXPRESS_CALLBACK_URL to the registered value."),
    ("AppWhiteIpLimit", "Your app restricts calls to whitelisted IPs. Add this machine's IP in the AliExpress console."),
    ("InvalidAppKey", "ALIEXPRESS_APP_KEY is wrong for this endpoint's region. Try --auth-host oauth."),
    ("permission", "The app lacks the Dropshipping permission. That is an account-level grant, not something this script can fix."),
    ("ApiCallLimit", "Rate limited. Wait a minute and retry."),
)


def _explain(body: str) -> None:
    lowered = body.casefold()
    hints = [hint for needle, hint in _HINTS if needle.casefold() in lowered]
    if hints:
        print("\nLikely cause:")
        for hint in dict.fromkeys(hints):
            print(f"  - {hint}")


AUTH_HOSTS = {
    "sg": "https://api-sg.aliexpress.com/oauth/authorize",
    "oauth": "https://oauth.aliexpress.com/authorize",
}


def consent_url(host: str, app_key: str, callback: str, both_redirects: bool) -> str:
    """Build the consent URL.

    ``both_redirects`` sends redirect_uri *and* redirect_url. AliExpress's docs disagree
    about which it wants, but some endpoints reject unexpected parameters outright — so
    the both-at-once workaround can itself be the failure. Default to redirect_uri alone
    and offer the pair as a fallback rather than the other way round.
    """
    params = {
        "response_type": "code",
        "force_auth": "true",
        "redirect_uri": callback,
        "client_id": app_key,
    }
    if both_redirects:
        params["redirect_url"] = callback
    return f"{host}?" + urllib.parse.urlencode(params)


def check_token(app_key: str, app_secret: str, product_id: str, debug: bool) -> int:
    """Call ds.product.get with the configured token and report the real error.

    This is how you tell "the token expired" apart from "the app was never granted the
    Dropshipping permission" — two very different problems that both surface as an
    empty Spare Images column.
    """
    token = os.environ.get("ALIEXPRESS_ACCESS_TOKEN", "").strip()
    if not token:
        print("ALIEXPRESS_ACCESS_TOKEN is not set — nothing to check.")
        print("Run this script with no arguments to mint one.")
        return 1
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ali_api  # noqa: PLC0415 - imported here so --check is the only path needing it

    print(f"Calling ds.product.get for product {product_id} …")
    try:
        detail = ali_api.get_product_detail(product_id)
    except Exception as exc:  # noqa: BLE001 - the message is the whole point
        print(f"\nFAILED: {exc}")
        _explain(str(exc))
        return 2
    base = detail.get("ae_item_base_info_dto", {}) if isinstance(detail, dict) else {}
    images = ali_api._images(detail) if isinstance(detail, dict) else []
    print("\nOK — the token works.")
    print(f"  title  : {str(base.get('subject', ''))[:70]}")
    print(f"  images : {len(images)} from ae_multimedia_info_dto")
    if debug:
        print(f"\n[debug] top-level keys: {sorted(detail)[:40]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--debug", action="store_true",
                        help="Print the signing base string and raw responses.")
    parser.add_argument("--check", action="store_true",
                        help="Test the existing ALIEXPRESS_ACCESS_TOKEN instead of minting a new one.")
    parser.add_argument("--check-product", default="3256808181943751",
                        help="Product ID used by --check.")
    parser.add_argument("--auth-host", choices=sorted(AUTH_HOSTS), default="sg",
                        help="Consent host. Try 'oauth' if 'sg' rejects your app key.")
    parser.add_argument("--both-redirects", action="store_true",
                        help="Send redirect_uri AND redirect_url. Try this only if redirect_uri alone fails.")
    args = parser.parse_args()

    app_key = _require("ALIEXPRESS_APP_KEY")
    app_secret = _require("ALIEXPRESS_APP_SECRET")

    if args.check:
        return check_token(app_key, app_secret, args.check_product, args.debug)

    callback = os.environ.get("ALIEXPRESS_CALLBACK_URL", DEFAULT_CALLBACK).strip()
    host = os.environ.get("ALIEXPRESS_AUTH_URL", "").strip() or AUTH_HOSTS[args.auth_host]
    consent = consent_url(host, app_key, callback, args.both_redirects)

    print("\nUsing:")
    print(f"  client_id (app key): {app_key}")
    print(f"  redirect_uri       : {callback}")
    print(f"  consent host       : {host}")
    print("  ^ the redirect must EXACTLY match the Callback URL registered on your app.")
    print("    Override with ALIEXPRESS_CALLBACK_URL if it differs.")
    print("\n  If the consent page errors before you can approve, retry with:")
    other = "oauth" if args.auth_host == "sg" else "sg"
    print(f"    --auth-host {other}          (the other regional consent host)")
    print("    --both-redirects          (send redirect_url as well as redirect_uri)")
    print("\n1) Open this URL, sign in to AliExpress and approve:\n")
    print(consent)
    print("\n2) You will land on your callback page. Copy the COMPLETE URL from the")
    print("   address bar (it contains ?code=...), then paste it below.")
    print("   Do this promptly — the code is single-use and expires quickly.\n")

    pasted = getpass.getpass("Paste the complete callback URL (hidden): ").strip()
    if not pasted:
        return 1
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
    code = (query.get("code") or [""])[0].strip() or (pasted if "//" not in pasted else "")
    if not code:
        print("\nERROR: no ?code= found in that URL.")
        error = (query.get("error_description") or query.get("error") or [""])[0]
        if error:
            # The callback often carries the real reason in the query string.
            print(f"The callback URL reported: {error}")
            _explain(error)
        return 2

    path = "/auth/token/create"
    params = {
        "app_key": app_key,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "code": code,
    }
    params["sign"] = _sign(params, app_secret, path, debug=args.debug)
    payload = _post(TOKEN_URL, params, debug=args.debug)

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
        body = json.dumps(payload)
        print("\nERROR: the request succeeded but carried no access_token:")
        print(body[:1200])
        _explain(body)
        return 2

    print("\n" + "=" * 70)
    print("SUCCESS. Add this as the GitHub secret ALIEXPRESS_ACCESS_TOKEN:\n")
    print(token)
    if expires:
        print(f"\n(expires: {expires} — re-run this script and update the secret when it lapses)")
    print("\nThen verify it actually works:")
    print("    ALIEXPRESS_ACCESS_TOKEN='<the value above>' python3 mint_ali_token.py --check")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
