# Daily AliExpress → eBay auto-lister

Sources **2 AliExpress products/day** via the official AliExpress API, lists both
**live on eBay** (main images only, shipping from your existing eBay policy), and
**emails you** the result with each product's AliExpress link and live eBay link.
Runs unattended in GitHub Actions (your computer can be off). Successful live
listings are also upserted into the workbook's **Auto Lister** tab. Cost ≈ $0/month.

The pipeline is Python 3.11. Google service-account authentication uses
`google-auth`; application API calls otherwise use the standard library.

## How it works

`find-and-prepare-ebay-listings/scripts/daily_run.py` is the daily entry point:

1. Picks the day's niche (5-niche rotation, `daily_history.py`).
2. Sources 2 qualifying products via `ali_api.py` (rating/orders/price/US gates,
   brand exclusions, history de-dup) and writes a `source.json` for each. Selection
   is **AI-free by default**: within the day's rotated niche it picks the two top
   **bestsellers** (highest AliExpress sales volume, functionally distinct).
3. Prepares + publishes both listings through the official eBay Sell APIs
   (`ebay_listing.py`: images → eBay Picture Services, category/aspects, qty 1,
   your fulfillment/payment/return policies, then publish + 10% promotion).
4. Upserts live listings into Google Sheets (`sheet_sync.py`), queuing transient
   failures under `state/` for replay on the next run.
5. Emails a report (`notify.py`) including the sheet-sync result.
6. The workflow commits updated history and pending-sync state back to the repo.

> Note: eBay policy compliance is your responsibility. Publishing attaches a
> mandatory **10% Promoted Listings (General/CPS)** ad to each listing.

## One-time setup

### 1. eBay
1. Create an eBay Developer **Production** keyset and a RuName using the OAuth
   pages already deployed at `https://akshayv10.github.io/listing-oauth-pages`.
2. On your Mac, from `find-and-prepare-ebay-listings/scripts/`:
   ```bash
   python3 ebay_setup.py authorize        # mints an ~18-month refresh token (Keychain)
   python3 ebay_setup.py configure-account --apply \
     --payment-policy-id … --return-policy-id … --fulfillment-policy-id …
   python3 ebay_setup.py preflight         # should print "ready"
   ```
   Pick the **fulfillment policy that matches your manual eBay shipping settings**.
3. Copy `ebay-account.example.json` to `ebay-account.json` and fill in the policy
   IDs / campaign ID that `preflight` reported. Commit `ebay-account.json`
   (it contains no secrets — only account identifiers).
4. Read the refresh token and client credentials for the GitHub Secrets below.
   (`ebay_setup.py authorize` stored them in macOS Keychain under service
   `find-and-prepare-ebay-listings.production`.)

### 2. AliExpress
Register an app on the AliExpress Open Platform as a **Dropshipping (individual)**
developer. Sourcing uses the **DS API** (`aliexpress.ds.product.get` for the real
star rating / review count / sales count / price / main images, and
`aliexpress.ds.freight.calculate` for real US shipping cost). Note the App Key,
App Secret, and Tracking ID.

> If the granted app exposes a different product-discovery method, set
> `ALI_DS_DISCOVERY` (`auto` | `text` | `feed`) and, for feed mode,
> `ALI_DS_FEED_NAME`. The authoritative gating (`ds.product.get`) is method-agnostic.

### 3. Email
Easiest: a Gmail **App Password** (Google Account → Security → App passwords).

- `SMTP_USER` / `SMTP_PASS` — the Gmail account that **sends** the report, and its app password
- `NOTIFY_FROM` — the "from" address (normally the same as `SMTP_USER`)
- `NOTIFY_EMAIL` — **where reports are delivered.** Change this secret to change the recipient.

Sender and recipient are independent: you can send from one account and receive at another.

### 4. Google Sheets
1. In Google Cloud, create a project (or select an existing one) and enable the
   **Google Sheets API**.
2. Create a service account and download one JSON key.
3. Share the target workbook with the service account's `client_email` as
   **Editor**.
4. Save the complete JSON key as the GitHub Actions secret
   `GOOGLE_SERVICE_ACCOUNT_JSON`.

The scheduled workflow writes only to a separate `Auto Lister` tab in spreadsheet
`10GgtsN_cxhHBvbEYa4vUXBUbC-LqeElkzmRiL3TT0Uk`. It does not modify the legacy
`Ebay` tab.

### 5. GitHub
Push this repo (private), then add **Settings → Secrets and variables → Actions**:

| Secret | Value |
| --- | --- |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` / `EBAY_RUNAME` | Production keyset + RuName |
| `EBAY_REFRESH_TOKEN` | from `ebay_setup.py authorize` |
| `ALIEXPRESS_APP_KEY` / `ALIEXPRESS_APP_SECRET` / `ALIEXPRESS_TRACKING_ID` | AliExpress app |
| `SMTP_USER` / `SMTP_PASS` | Gmail address + app password |
| `NOTIFY_FROM` | usually same as `SMTP_USER` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | complete service-account key JSON |
| `OPENAI_API_KEY` | OpenAI key for AI-written eBay title/description/specifics (gpt-4.1-mini) |
| `ALIEXPRESS_ACCESS_TOKEN` | Seller token for `ds.product.get` (review count + authoritative rating, variants) + per-SKU freight. Mint with `mint_ali_token.py`. **Required for the daily run** — without it the review/rating gates can't be verified, so every candidate is rejected and nothing lists. |

Optional **Variables**: `RUN_TZ` (default `Asia/Kolkata`), `SMTP_HOST`, `SMTP_PORT`,
`OPENAI_MODEL` (default `gpt-4.1-mini`). Without `OPENAI_API_KEY` the listings still
publish, using a plain template description instead of AI copy.

### 6. Running it (automation is currently PAUSED)
There is **no schedule** — runs are manual only. **Actions → Run workflow**, then pick a
mode (the default publishes):

- `full` (default): run the production listing pipeline — **publishes real eBay listings**
- `dry-run`: source and validate without eBay, email, or Sheets writes — **safe testing**
- `sheet-sync-only`: create/repair `Auto Lister`, replay queued rows, and backfill history
- `email-test`: send a harmless test message without creating a listing

Because `full` is the default, clicking **Run workflow** without changing the input
publishes. Pick `dry-run` when you only want to test.

Notes:

- The daily run needs `ALIEXPRESS_ACCESS_TOKEN` set, or it sources nothing (see the secrets
  table above).
- Locally, `python3 daily_run.py` is always a dry run; only `--live` publishes.

To enable the daily schedule later, uncomment the two `schedule` lines in
`.github/workflows/daily.yml` and set the cron to your time **in UTC**. Scheduled runs
already publish — they are included in the LIVE condition in the run step.

## Prepare a verified pair from `find-resale-products`

`.github/workflows/handoff.yml` accepts the exact two-product batch produced by the
installed `find-resale-products` skill. It is intentionally separate from daily
auto-sourcing and the advisory single-link inbox path.

The prepare dispatch:

1. re-fetches both product IDs through the AliExpress DS API;
2. hard-enforces rating, reviews, orders, US-region, image, brand, and current
   exact-variant price gates;
3. resolves the browser-selected option values to one current AliExpress SKU;
4. creates unpublished eBay offers independently; and
5. uploads a seven-day `prepared-<frp-run-id>` review artifact.

Nothing is published during prepare. A later publish dispatch must provide both the
prepare Actions run ID and the exact reviewed `frp-...` logical run ID. Each product is
then published and promoted independently: a successful listing remains live if its
sibling fails, while a product whose mandatory 10% General promotion fails is withdrawn.
Ambiguous mutations are marked for read-only reconciliation and are never blindly
retried.

The batch payload is passed as base64 JSON through `workflow_dispatch` and is not
committed. Successful live listing history continues to be committed under `state/`.
The local skill uses `gh workflow run ... --json`, waits for the result, and reconciles
verified live item IDs and URLs back to the local resale history.

## List a link on demand (email)

Besides the daily auto-sourcing, you can list a **specific** AliExpress product you found
by emailing yourself a link. A separate workflow (`.github/workflows/inbox.yml`) polls the
inbox, lists the linked product, and replies with the live eBay link.

**How to use it**
1. From the same address the lister emails (`NOTIFY_EMAIL`), send an email with subject:
   `LIST: https://www.aliexpress.us/item/<id>.html` (the URL can also be in the body).
2. Within the poll interval, the workflow fetches that product, lists it, and replies with
   the eBay link — reusing the exact same pipeline as the daily run (images, variants,
   pricing, 10% promotion).

**Safety (starts paused / dry-run, like the daily workflow)**
- Only emails **from an authorized sender** (defaults to `NOTIFY_EMAIL` / `NOTIFY_FROM` /
  `SMTP_USER` — i.e. you) **and** with the `LIST:` subject tag are acted on. Everything else
  is left untouched. Override the allow-list with `INBOX_ALLOWED_SENDERS` (comma-separated).
- The `From` header is spoofable, so for live use set a secret **`INBOX_SECRET`** (a repo
  secret): a `LIST:` email must then also contain that token in the subject or body before
  anything is published. Recommended once you flip publishing on.
- Publishing is opt-in: set repository **variable `LIVE_LISTING=1`** to actually list.
  Without it every run is a dry run that reads mail and validates but lists nothing — and a
  dry run **leaves the request unread** (pending), so nothing is lost before you go live.
- To poll automatically, uncomment the `schedule` line in `inbox.yml` (every 15 min).
  You can also run it any time via **Actions → Inbox link lister → Run workflow**.
- **Quality gates are advisory here**: because you hand-picked the product, a gate miss
  (e.g. few reviews) is reported as a warning in the reply email but the item is still
  listed. Only unlistable products (no id/title/price/images) are refused.

Login reuses your existing Gmail **app password** (`SMTP_USER` / `SMTP_PASS`) over IMAP —
no new secrets. Optional variables: `IMAP_HOST` (default `imap.gmail.com`), `IMAP_PORT`
(`993`), `INBOX_SUBJECT_TAG` (default `LIST:`).

Run the single-URL lister directly (dry run by default; `--live` publishes):

```bash
cd find-and-prepare-ebay-listings/scripts
python3 list_from_url.py "https://www.aliexpress.us/item/<id>.html"
python3 inbox_poll.py            # dry-run poll of the inbox
```

## Testing offline (no network, no eBay)

```bash
cd find-and-prepare-ebay-listings/scripts
python3 test_ali_api.py            # sourcing/gates/mapping
python3 test_list_from_url.py      # on-demand single-URL lister (URL parse, gate warning)
python3 test_skill.py              # eBay-side regression (never hits Production)
ALI_API_FIXTURE="$PWD/fixtures/ali_sample.json" \
  HISTORY_PATH=/tmp/h.jsonl RUNS_DIR=/tmp/runs \
  python3 daily_run.py --dry-run   # full pipeline, writes source.json, prints nothing to eBay
```

## Tuning (environment variables)

`ALI_MIN_RATING` (4.5), `ALI_MIN_REVIEWS` (25), `ALI_MIN_ORDERS` (100),
`ALI_MIN_PRICE_USD` (15), `ALI_USE_FREIGHT` (1), `ALI_SHIPPING_PCT` /
`ALI_SHIPPING_FLAT` (delivered-cost estimate when freight lookup is unavailable),
`ALI_DS_DISCOVERY` (auto|text|feed), `ALI_DS_FEED_NAME`.
Niche search queries live in `ali_api.py` (`NICHE_QUERIES`).

Product selection is deterministic (top bestsellers by sales volume within the
day's niche) unless `ALI_AI_RANK=1` is set, which opts into AI-scored resale-appeal
ranking (needs `OPENAI_API_KEY`); any AI failure falls back to the deterministic
bestseller ranker.
