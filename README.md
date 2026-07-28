# Daily AliExpress → eBay draft-and-publish lister

Sources **2 AliExpress products/day** via the official AliExpress API, prepares a
complete eBay listing for each, and **waits**. Nothing goes live on a schedule. When
you're ready — that afternoon, that weekend, whenever — you press one button and the
day's batch is listed. You then adjust the live listings on eBay however you like.

The point is timing: you are never made to drop what you're doing because a cron fired.

Runs unattended in GitHub Actions (your computer can be off). Live listings are upserted
into the workbook's **Auto Lister** tab. Cost ≈ $0/month.

If you'd rather go back to fully automatic listing at a fixed time, set the repository
variable `LISTING_MODE=auto` (see [Going back to automatic](#going-back-to-automatic)).

The pipeline is Python 3.11. Google service-account authentication uses
`google-auth`; application API calls otherwise use the standard library.

## How it works

Two steps, and the gap between them is yours.

### Step 1 — the daily run prepares drafts

`daily_run.py --draft` runs on the schedule:

1. Picks the day's niche (5-niche rotation, `daily_history.py`).
2. Sources 2 qualifying products via `ali_api.py` (rating/orders/price/US gates, brand
   exclusions, history de-dup), picking the day's top **bestsellers**. Products already
   drafted are excluded, so you never get the same item twice.
3. Enriches each — real AliExpress variations plus the AI-written title, description and
   item specifics — so the draft is a finished listing.
4. Validates against eBay **read-only**: category and required item specifics via the
   Taxonomy API. **Nothing is created on eBay.** No images uploaded, no offer written.
5. Saves each draft to `state/drafts/<draft-id>.json`, writes a row to the **Drafts**
   status board, and emails you — including any older drafts still waiting.

### Step 2 — you press the button

**Actions → Publish approved eBay drafts → Run workflow.** That's it; publishing is the
default, so you don't change any dropdown.

It publishes **the most recent day's batch** and reports anything older that's still
waiting, so one press is a predictable two listings rather than a week's accumulation.
Per draft it:

1. **re-checks the AliExpress cost** — a draft may have sat for days, so if the item is
   gone or delivered cost rose past `DRAFT_MAX_COST_DRIFT_PCT` (default 10%), it refuses
   that one rather than listing at a dead margin;
2. lists it through the same eBay Sell API chain as always (images → eBay Picture
   Services, category/aspects, qty 1, your policies, publish + 10% promotion), with AI
   enrichment **off** so the copy that goes live is the copy that was drafted;
3. records history, upserts the **Auto Lister** row, updates the status board, and emails.

Drafts are independent — one failure never blocks the others.

To clear a backlog in one go, set **scope** to `all`.

### When eBay refuses a product

Some products eBay simply won't take. A real example from this repo: a *Dr Pen Ultima
M8S* microneedling pen — a branded medical device listed as `Unbranded` — came back with
*"the listing or seller may be in violation of eBay policy"*.

Such a draft is retried once more, then **parked**: it stops being picked up
automatically and is reported as needing attention, so it doesn't fail noisily on every
press. Force it anyway with `--draft-id`, or drop it with
`python3 draft_store.py reject <draft-id>`.

## The Drafts tab is a status board

One row per draft: which **Batch** it belongs to, its status, cost, price, warnings, and
once live its eBay link. **Nothing is read back from it** — publishing selects from the
draft records under `state/drafts/`, so editing a cell changes nothing and a Sheets
outage can't stop a publish.

Adjust listings on eBay after they go live.

## A note on `collect_images.py`

`scripts/collect_images.py` fetches a product's full image set from the AliExpress page
(the API can't reach it without a seller token, and GitHub Actions is bot-blocked, so it
runs on your Mac). It writes into the Drafts tab.

**Under the current flow it does not change what gets published.** Publishing reads the
draft records under `state/drafts/`, not the sheet, so anything the collector writes to
the sheet is cosmetic. It's left in place but unwired — add photos on eBay after the
listing goes live instead. Rewiring it to update the draft records would be a small
change if you ever want it.

### The seller token is worth another try

Most of the above exists to work around a missing `ALIEXPRESS_ACCESS_TOKEN`. Without it
you also lose multi-variant listings, per-SKU shipping costs, and the review-count gate.
The minter now reports AliExpress's actual error instead of a bare `HTTP 400`:

```bash
python3 mint_ali_token.py --debug          # prints the signing base string and real error
python3 mint_ali_token.py --auth-host oauth   # the other regional consent host
python3 mint_ali_token.py --both-redirects    # if redirect_uri alone is rejected
ALIEXPRESS_ACCESS_TOKEN='…' python3 mint_ali_token.py --check   # does an existing token work?
```

`--check` distinguishes "the token expired" from "the app was never granted the
Dropshipping permission" — two problems that look identical from the sheet.

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

**Actions → Publish approved eBay drafts** is the second step, and it defaults to
`publish` — running it lists the newest batch. Pick `dry-run` to resolve and validate
without listing. Set **scope** to `all` to clear a backlog, or pass a `draft_id` to
publish one specific draft (including a parked one).

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
python3 test_collect_images.py     # image collection: classification, ordering, caps
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
python3 draft_sheet.py pending              # what is queued, and what is parked
python3 publish_drafts.py                   # dry run of the newest batch
python3 publish_drafts.py --live            # publish the newest batch
python3 publish_drafts.py --live --all      # clear the whole backlog
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
