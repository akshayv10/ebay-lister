# Daily AliExpress → eBay draft-and-publish lister

Sources **2 AliExpress products/day** via the official AliExpress API, prepares a
complete eBay listing for each, and **emails you a draft to review**. You check it,
change anything you want, tick a box, and then it goes live. Runs unattended in
GitHub Actions (your computer can be off). Successful live listings are upserted into
the workbook's **Auto Lister** tab. Cost ≈ $0/month.

**Nothing is listed without your approval** — this is the default. If you'd rather go
back to fully automatic listing, set the repository variable `LISTING_MODE=auto`
(see [Going back to automatic](#going-back-to-automatic)).

The pipeline is Python 3.11. Google service-account authentication uses
`google-auth`; application API calls otherwise use the standard library.

## How it works

There are two steps, and you are the gate between them.

### Step 1 — the daily draft run

`find-and-prepare-ebay-listings/scripts/daily_run.py --draft` runs on the schedule:

1. Picks the day's niche (5-niche rotation, `daily_history.py`).
2. Sources 2 qualifying products via `ali_api.py` (rating/orders/price/US gates,
   brand exclusions, history de-dup). Selection is **AI-free by default**: within the
   day's rotated niche it picks the two top **bestsellers** (highest AliExpress sales
   volume, functionally distinct).
3. Enriches each one — real AliExpress variations plus the AI-written title,
   description, and item specifics — so the draft you review is the finished listing.
4. Validates each against eBay **read-only**: resolves the category and required item
   specifics through the Taxonomy API. **Nothing is created on eBay.** No images are
   uploaded, no inventory item or offer is written.
5. Saves each draft to `state/drafts/<draft-id>.json`, writes a row into the workbook's
   **Drafts** tab, and renders an HTML review page.
6. Emails you "📝 N drafts ready to review" with that page inline, so you can check the
   photos, price, and copy from your phone. The same page is uploaded as a workflow
   artifact.

### Step 2 — you review and approve

Open the **Drafts** tab, change whatever you like, set `Publish?` to `YES` on the rows
you want, then run **Actions → Publish approved eBay drafts** with mode `publish`.

`publish_drafts.py` then, per approved draft:

1. layers your edits onto the stored draft;
2. **re-checks the AliExpress cost** — if the item is gone, or delivered cost rose more
   than `DRAFT_MAX_COST_DRIFT_PCT` (default 10%), it refuses that draft and tells you,
   rather than listing at a price that no longer earns its margin;
3. lists it through the same eBay Sell API chain as before (images → eBay Picture
   Services, category/aspects, qty 1, your fulfillment/payment/return policies, then
   publish + 10% promotion), **with AI enrichment switched off** so your edits are never
   overwritten;
4. records history, upserts the **Auto Lister** row, writes the live eBay link back into
   the Drafts row, and emails the result.

Drafts are independent: one failing never blocks the others. A draft that fails or gets
blocked keeps your edits and your tick so you can correct it and re-run; a draft that
went live can never be published twice. Products already sitting in review are excluded
from the next day's sourcing, so you never get two drafts for the same item.

The `dry-run` mode does the same resolution — your edits, the cost re-check, and full
validation — and just stops before listing. It reports how many drafts *would* publish
and why the rest would be refused.

> Note: eBay policy compliance is your responsibility. Publishing attaches a
> mandatory **10% Promoted Listings (General/CPS)** ad to each listing.

> **Why isn't the draft in eBay Seller Hub?** Because eBay has no public API that
> creates one. Unpublished Inventory API offers are invisible in Seller Hub, Trading
> `AddItem` publishes immediately, and the Sell Listing API's `createItemDraft` is a
> limited release for approved partner integrations only. The Drafts tab is the draft.

## Reviewing a draft

Each draft is one row in the **Drafts** tab. You own these columns:

| Column | What to put in it |
| --- | --- |
| `Publish?` | `YES` to approve. Anything else (blank, `NO`) is left alone. Cleared back to `NO` once the listing is live, so an approval is never left standing. |
| `Title` | eBay title, max 80 characters. |
| `Description` | Listing description (HTML is fine). |
| `Category ID` | Pin an exact eBay category instead of the suggested one. |
| `Item Specifics` | One per line, `Name: value` (`Color: Red, Blue` for multiple values). |
| `Images` | One image URL per line — this is the listing's gallery, in order. |
| `Variants` | One per line: `id \| Color=Red, Size=L \| visible_price \| delivered_cost`. |
| `Price Override USD` | Set the eBay price by hand. Must be above delivered cost. |

Everything else (`Status`, `Thumbnail`, `Delivered Cost`, `Suggested Price`, `Warnings`,
`eBay Listing ID`, `eBay URL`, `Publish Error`) is written by the pipeline and refreshed
on every sync. Clearing an editable cell falls back to the drafted value rather than
wiping the field.

### Adding photos the AliExpress API can't reach

`ds.product.get` returns the main gallery and, with a seller token, per-SKU thumbnails —
but not the lifestyle and description shots. So each draft also scrapes the public
AliExpress product page and puts everything it found that the listing **isn't** using
into a read-only **Spare Images** column. Adding one is a copy from that cell into
`Images`.

Anything else you paste into `Images` works too, as long as it's a public HTTPS URL —
that's an eBay requirement, not ours: eBay's importer fetches the image from the URL you
give it, so a local file or a Google Drive share link won't work.

## Going back to automatic

The original publish-immediately pathway is untouched and still available:

- **For the schedule:** set the repository variable `LISTING_MODE` to `auto`. Scheduled
  runs then publish both products immediately, exactly as before. Delete the variable (or
  set it to `draft`) to go back to reviewing.
- **For one run:** **Actions → Daily eBay auto-lister → Run workflow → mode: `full`**.
- **Locally:** `python3 daily_run.py --live`.

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
| `ALIEXPRESS_ACCESS_TOKEN` | Seller token for `ds.product.get` (review count + authoritative rating, variants) + per-SKU freight. Mint with `mint_ali_token.py`. Optional but recommended — without it the daily run still lists from feed data (orders + approximate rating), but the review-count gate is skipped and listings are single-variation. Set it to enforce the 50-review / authoritative-rating gates. |

Optional **Variables**: `RUN_TZ` (default `Asia/Kolkata`), `SMTP_HOST`, `SMTP_PORT`,
`OPENAI_MODEL` (default `gpt-4.1-mini`), `LISTING_MODE` (`draft` by default; `auto`
publishes on the schedule without review), `SHEETS_DRAFT_TAB_NAME` (default `Drafts`),
`DRAFT_MAX_COST_DRIFT_PCT` (default `10`). Without `OPENAI_API_KEY` the listings still
publish, using a plain template description instead of AI copy.

### 6. Running it
The daily workflow runs at **09:00 IST (03:30 UTC)** and, by default, **drafts for
review** — it lists nothing on its own.

**Actions → Daily eBay auto-lister → Run workflow**, then pick a mode:

- `draft` (default): source, enrich, validate, save drafts — **nothing goes live**
- `full`: run the production listing pipeline — **publishes real eBay listings immediately**
- `dry-run`: source and validate without eBay, email, or Sheets writes — **safe testing**
- `sheet-sync-only`: create/repair `Auto Lister`, replay queued rows, and backfill history
- `email-test`: send a harmless test message without creating a listing

**Actions → Publish approved eBay drafts** is the second step. It defaults to `dry-run`
(resolve the approved drafts and validate your edits, list nothing); pick `publish` to go
live. An optional `draft_id` input publishes just one draft — it must still be ticked in
the sheet.

Notes:

- The daily run needs `ALIEXPRESS_ACCESS_TOKEN` set, or it sources nothing (see the secrets
  table above).
- Locally, `python3 daily_run.py` is always a dry run; `--draft` saves drafts and only
  `--live` publishes. Same for `publish_drafts.py`: `--live` is required to list.
- To pause everything, comment out the `schedule` block in `daily.yml` or disable the
  workflow in the Actions tab.

To enable the daily schedule later, uncomment the two `schedule` lines in
`.github/workflows/daily.yml` and set the cron to your time **in UTC**. Scheduled runs
already publish — they are included in the LIVE condition in the run step.

## Prepare a verified pair from `find-resale-products`

`.github/workflows/handoff.yml` accepts the exact two-product batch produced by the
installed `find-resale-products` skill. It supports legacy reviewed preparation and a
schema-version-2 immediate-list operation carrying eBay-hosted AliSave image manifests.

The prepare dispatch:

1. re-fetches both product IDs through the AliExpress DS API;
2. hard-enforces rating, reviews, orders, US-region, image, brand, and current
   exact-variant price gates;
3. resolves the browser-selected option values to one current AliExpress SKU;
4. creates unpublished eBay offers independently; and
5. uploads a seven-day `prepared-<frp-run-id>` review artifact.

Nothing is published during legacy prepare. A later publish dispatch must provide both the
prepare Actions run ID and the exact reviewed `frp-...` logical run ID. Each product is
then published and promoted independently: a successful listing remains live if its
sibling fails, while a product whose mandatory 10% General promotion fails is withdrawn.
Ambiguous mutations are marked for read-only reconciliation and are never blindly
retried.

For schema version 2, the Mac first uploads compliant AliSave files through eBay Media
API `createImageFromFile`. The workflow receives only EPS IDs/URLs and hashes—never
local paths or image bytes. Operation `list` prepares and publishes each valid product
in the same Action after verifying the exact deterministic run ID. A valid sibling
remains live if the other product fails.

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

Every `test_*.py` runs automatically on pull requests and pushes to `main`
(`.github/workflows/tests.yml`). No secrets are provided to that job, so a test that
tried to reach a real API would fail rather than do something real. To run the same
thing locally:

```bash
cd find-and-prepare-ebay-listings/scripts
for t in test_*.py; do python3 "$t" || echo "FAILED: $t"; done
```

Individually:

```bash
cd find-and-prepare-ebay-listings/scripts
python3 test_ali_api.py            # sourcing/gates/mapping
python3 test_list_from_url.py      # on-demand single-URL lister (URL parse, gate warning)
python3 test_skill.py              # eBay-side regression (never hits Production)
python3 test_drafts.py             # draft flow: edits, price override, stale-cost guard
python3 test_safety.py             # dry-run-by-default and draft-by-default posture
ALI_API_FIXTURE="$PWD/fixtures/ali_sample.json" \
  HISTORY_PATH=/tmp/h.jsonl RUNS_DIR=/tmp/runs \
  python3 daily_run.py --dry-run   # full pipeline, writes source.json, prints nothing to eBay

# Draft flow end-to-end, offline: writes /tmp/drafts/*.json and a review page you can open.
ALI_API_FIXTURE="$PWD/fixtures/ali_sample.json" \
  HISTORY_PATH=/tmp/h.jsonl RUNS_DIR=/tmp/runs DRAFT_DIR=/tmp/drafts \
  DRAFT_PREVIEW_PATH=/tmp/preview.html DRAFT_SHEET_DISABLED=1 \
  python3 daily_run.py --draft --no-email
```

## Inspecting drafts from the command line

```bash
cd find-and-prepare-ebay-listings/scripts
python3 draft_store.py list                 # every draft, newest first
python3 draft_store.py show <draft-id>      # the full draft record
python3 draft_store.py reject <draft-id>    # take one out of the running
python3 draft_sheet.py sync                 # rebuild the Drafts tab from state/drafts/
python3 draft_sheet.py approved             # which draft IDs are currently ticked
python3 publish_drafts.py                   # dry run: what would be published
```

## Tuning (environment variables)

`ALI_MIN_RATING` (4.5), `ALI_MIN_REVIEWS` (25), `ALI_MIN_ORDERS` (100),
`ALI_MIN_PRICE_USD` (15), `ALI_USE_FREIGHT` (1), `ALI_SHIPPING_PCT` /
`ALI_SHIPPING_FLAT` (delivered-cost estimate when freight lookup is unavailable),
`ALI_DS_DISCOVERY` (auto|text|feed), `ALI_DS_FEED_NAME`.
Niche search queries live in `ali_api.py` (`NICHE_QUERIES`).

Draft flow: `DRAFT_DIR` (`state/drafts`), `SHEETS_DRAFT_TAB_NAME` (`Drafts`),
`DRAFT_MAX_COST_DRIFT_PCT` (`10` — how far delivered cost may rise between drafting and
publishing), `DRAFT_PREVIEW_PATH`, `DRAFT_SHEET_DISABLED` (skip all Sheets calls),
`DRAFT_VERIFY_IMAGES` (also test-import every image into eBay Picture Services at draft
time; off by default because it creates hosted EPS images).

Product selection is deterministic (top bestsellers by sales volume within the
day's niche) unless `ALI_AI_RANK=1` is set, which opts into AI-scored resale-appeal
ranking (needs `OPENAI_API_KEY`); any AI failure falls back to the deterministic
bestseller ranker.
