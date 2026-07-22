# eBay Production API setup

## Contents

1. Public OAuth pages
2. Developer application
3. Local authorization
4. Seller-account configuration
5. Safety boundaries

## Public OAuth pages

Use the dedicated Production project at `https://ebay-oauth-pages.vercel.app`:

- privacy: `https://ebay-oauth-pages.vercel.app/privacy.html`
- accepted: `https://ebay-oauth-pages.vercel.app/accepted.html`
- declined: `https://ebay-oauth-pages.vercel.app/declined.html`

The accepted page runs no backend. It only copies its complete browser URL so the local helper can extract the short-lived authorization code. Do not paste that URL into chat or a shell argument.

## Developer application

Create an eBay Developer account and Production keyset. Configure a Production RuName with the deployed privacy, accepted, and declined HTTPS URLs. Request only the scopes emitted by `ebay_common.py`: base, `sell.account`, `sell.inventory`, `sell.marketing`, and `sell.metadata`.

Production consent is required because unpublished Inventory API offers are seller-owned records. Sandbox tokens and Production tokens are not interchangeable.

## Local authorization

Run `python3 scripts/ebay_setup.py authorize` in an interactive terminal. Enter the Production Client ID, Client Secret, and RuName when prompted. Approve the printed eBay consent URL, then paste the complete accepted-page URL into the hidden prompt.

The helper stores Client ID, Client Secret, RuName, and refresh token as separate generic-password items under macOS Keychain service `find-and-prepare-ebay-listings.production`. It passes secret values to Keychain through standard input, never process arguments. Access tokens remain in process memory.

Use `reauthorize` after revoked or expired consent. A failed reauthorization must not delete the last working refresh token.

## Seller-account configuration

Run `preflight`. Inspect the returned policy IDs and names. Select one existing EBAY_US payment policy, return policy, and domestic free-shipping fulfillment policy. Do not invent handling or return terms.

Run `configure-account --apply` with policy IDs when multiple compatible policies exist. The helper opts into Selling Policy Management when necessary, then creates or enables:

- merchant location key: `irvine-92618`
- physical location: entered interactively during setup and sent only to eBay; never stored in this skill, config, or reports
- dedicated campaign: `Codex API Listings 10 Percent`
- funding model: General / Cost Per Sale
- fixed bid: 10.0%

The local account config contains only non-secret policy IDs, location key, campaign ID/name, marketplace, locale, and promoted rate. It must never contain the street address or OAuth material.

## Safety boundaries

- `status` reads only Keychain presence and non-secret config.
- `preflight` performs only GET requests.
- `configure-account` requires `--apply` because it can opt into a program, create the location, and create the campaign.
- Never create payment, return, or fulfillment policy terms automatically.
- A missing compatible policy or Marketing eligibility is a blocker. Use the extension backup only after a separate explicit request.
