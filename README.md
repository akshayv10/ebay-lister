# Daily AliExpress → eBay auto-lister

Sources **2 AliExpress products/day** via the official AliExpress API, lists both
**live on eBay** (main images only, shipping from your existing eBay policy), and
**emails you** the result with each product's AliExpress link and live eBay link.
Runs unattended in GitHub Actions (your computer can be off). Cost ≈ $0/month.

Everything is Python 3.11 standard library — no third-party packages.

## How it works

`find-and-prepare-ebay-listings/scripts/daily_run.py` is the daily entry point:

1. Picks the day's niche (5-niche rotation, `daily_history.py`).
2. Sources 2 qualifying products via `ali_api.py` (rating/orders/price/US gates,
   brand exclusions, history de-dup) and writes a `source.json` for each.
3. Prepares + publishes both listings through the official eBay Sell APIs
   (`ebay_listing.py`: images → eBay Picture Services, category/aspects, qty 1,
   your fulfillment/payment/return policies, then publish + 10% promotion).
4. Emails a report (`notify.py`).
5. The workflow commits updated history in `state/` back to the repo.

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
`SMTP_USER` = your Gmail address, `SMTP_PASS` = the app password.

### 4. GitHub
Push this repo (private), then add **Settings → Secrets and variables → Actions**:

| Secret | Value |
| --- | --- |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` / `EBAY_RUNAME` | Production keyset + RuName |
| `EBAY_REFRESH_TOKEN` | from `ebay_setup.py authorize` |
| `ALIEXPRESS_APP_KEY` / `ALIEXPRESS_APP_SECRET` / `ALIEXPRESS_TRACKING_ID` | AliExpress app |
| `SMTP_USER` / `SMTP_PASS` | Gmail address + app password |
| `NOTIFY_EMAIL` | where reports go (e.g. akshayv10@gmail.com) |
| `NOTIFY_FROM` | usually same as `SMTP_USER` |
| `OPENAI_API_KEY` | OpenAI key for AI-written eBay title/description/specifics (gpt-4.1-mini) |

Optional **Variables**: `RUN_TZ` (default `Asia/Kolkata`), `SMTP_HOST`, `SMTP_PORT`,
`OPENAI_MODEL` (default `gpt-4.1-mini`). Without `OPENAI_API_KEY` the listings still
publish, using a plain template description instead of AI copy.

### 5. Schedule
Edit the `cron` in `.github/workflows/daily.yml` to your desired time **in UTC**
(it currently runs 14:00 UTC). Trigger a manual run from the **Actions** tab
("Run workflow", optionally with **dry run** checked) to test.

## Testing offline (no network, no eBay)

```bash
cd find-and-prepare-ebay-listings/scripts
python3 test_ali_api.py            # sourcing/gates/mapping
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
