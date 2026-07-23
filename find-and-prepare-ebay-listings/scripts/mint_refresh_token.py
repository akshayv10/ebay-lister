#!/usr/bin/env python3
"""Mint an eBay OAuth refresh token from environment credentials and print it,
for pasting into GitHub Actions Secrets. No macOS Keychain involved.

Set these first (same shell), then run this script:
    export EBAY_CLIENT_ID='AkshayRa-Lister-PRD-xxxxxxxx'
    export EBAY_CLIENT_SECRET='PRD-xxxxxxxxxxxx'
    export EBAY_RUNAME='Akshay_Rao-AkshayRa-Lister-tpljbfs'
    python3 mint_refresh_token.py
"""

from __future__ import annotations

import base64
import getpass
import os
import sys

from ebay_common import Keychain, UrllibTransport, authorization_code, consent_url


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"ERROR: {name} is not set. Export it first (see the header of this file).")
    return value


def main() -> int:
    client_id = _require("EBAY_CLIENT_ID")
    client_secret = _require("EBAY_CLIENT_SECRET")
    runame = _require("EBAY_RUNAME")

    # consent_url reads client_id/runame from env (Keychain.get is env-first).
    consent = consent_url(Keychain())
    print("\n1) Open this URL, sign in, and approve:\n")
    print(consent)
    print("\n2) You will land on your accepted page. Copy the COMPLETE URL from the")
    print("   address bar (it contains ?code=...), then paste it below.\n")

    callback = getpass.getpass("Paste the complete accepted-page URL (hidden): ").strip()
    if not callback:
        return 1

    code = authorization_code(callback)
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = UrllibTransport().request(
        "POST",
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}"},
        form_body={"grant_type": "authorization_code", "code": code, "redirect_uri": runame},
    )
    if response.status != 200 or not isinstance(response.data, dict) or not response.data.get("refresh_token"):
        print("\nERROR: token exchange failed:", response.status, response.data)
        return 2

    refresh_token = response.data["refresh_token"]
    print("\n" + "=" * 70)
    print("SUCCESS. Add this as the GitHub secret EBAY_REFRESH_TOKEN:\n")
    print(refresh_token)
    print("\n(also export it now so you can run configure-account/preflight next:)")
    print(f"\n  export EBAY_REFRESH_TOKEN='{refresh_token}'")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
