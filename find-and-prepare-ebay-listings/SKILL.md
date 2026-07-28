---
name: find-and-prepare-ebay-listings
description: Find exactly two qualifying AliExpress resale products, prepare complete unpublished eBay US offers through the official eBay APIs, and publish them only after a separate explicit approval. Use for daily sourcing, eBay API setup/preflight, unpublished-offer review, approved two-listing publication, reconciliation, or an explicitly requested Chrome-extension fallback. Never purchase, never publish during preparation, and never switch to the extension automatically.
---

# Find and Prepare eBay Listings

Use the official eBay APIs by default. Find and initialize both products, prepare two unpublished offers, and stop for review. Publish only in a later turn after the user explicitly approves the reported run ID.

## Hard contract

- Never click `Place order`, `Pay now`, `Submit order`, or another purchase control.
- Keep `publish_allowed: false` throughout sourcing and preparation.
- Treat `ebay_listing.py publish` as a separate workflow requiring a new explicit user instruction and the exact prepared run ID.
- Publish exactly two products as one batch. If either listing or its 10% General promotion fails, remove newly created ads and withdraw the whole pair.
- For the dedicated `find-resale-products` GitHub handoff only, use the independent
  prepare/publish commands: a successful sibling may remain prepared or live when the
  other fails. Never apply that exception to the ordinary two-product API run.
- Require General/CPS promotion at exactly 10%; never create or join Priority/CPC promotion.
- Never expose Client ID, Client Secret, RuName, authorization code, access token, or refresh token in logs, commands, artifacts, or chat. Use macOS Keychain.
- Never include the full dispatch address in run artifacts. Use merchant location key `irvine-92618`.
- Never blindly repeat a timed-out mutating API call. Reconcile by deterministic SKU, offer, group, listing, or campaign identifier first.
- Never fall back to the Chrome extension after an API error. Use it only when the user explicitly requests the extension backup.
- Preserve exactly two functionally distinct products and the existing sourcing/browser budgets.

## 1. Choose the requested mode

Use `ebay_api` unless the user explicitly says to use or revert to the extension backup. For the backup, read [references/extension-backup.md](references/extension-backup.md) and follow it instead of the API stages. Never mix modes within one run.

## 2. Set up or verify the API

Read [references/ebay-api-setup.md](references/ebay-api-setup.md) when credentials are absent, authorization has expired, account configuration changes, or `preflight` does not return `ready`.

Run:

```bash
python3 scripts/ebay_setup.py status
python3 scripts/ebay_setup.py preflight
```

Do not source products until Production OAuth, compatible EBAY_US business policies, location `irvine-92618`, and the dedicated 10% General/CPS campaign all pass.

## 3. Source and initialize both products

Read [references/daily-sourcing.md](references/daily-sourcing.md). Create `ebay-listing-runs/<timestamp>/` with `candidate-ledger.jsonl`, `run-budget.json`, and one product directory per accepted product. Initialize and persist browser budgets with `run_budget.py`.

Apply gates in this order: search card; rating/reviews/sales/US availability and brand risk; history duplication; selected-variant visible price; eBay sold-listings demand; read-only checkout pricing. Record acceptance at `visible_price` when the selected single-unit price is at least USD 10. Require a positive delivered total before initializing a listing. Never purchase.

Capture only verified facts needed by [references/handoff-schema.md](references/handoff-schema.md): factual copy inputs, Brand, item specifics, 1–24 HTTPS images for a single-variation item or at most 12 for a variation group, and 1–4 real selected combinations. Generate factual title and description from this evidence without directly calling the OpenAI API. Run `listing_job.py init` for each source. Both results must exist before any eBay mutation.

## 4. Prepare two unpublished offers

Read [references/ebay-api-workflow.md](references/ebay-api-workflow.md). Run:

```bash
python3 scripts/ebay_listing.py prepare --run-dir <run-directory>
```

The helper must:

1. re-run account preflight;
2. validate both sources before the first API mutation;
3. resolve an eBay category and validate condition plus every required aspect;
4. import at least one image into eBay Picture Services;
5. create or replace deterministic inventory SKUs and an Inventory Item Group when needed;
6. create or reconcile unpublished fixed-price offers with quantity 1, free-shipping policies, deterministic prices, and location `irvine-92618`;
7. retrieve listing fees and read back every record; and
8. write `run-result.json` plus `review.md` with `status: api_prepared`, `published: false`, and `publish_allowed: false`.

Stop after reporting the two unpublished offers and their run ID. Do not infer Seller Hub draft availability; Inventory API records must be managed through the API.

## 5. Publish only after later approval

Enter this stage only when the latest user message explicitly approves the prepared run. Re-read `run-result.json`, require the exact run ID, and run:

```bash
python3 scripts/ebay_listing.py publish --run-dir <run-directory> --confirm-run-id <exact-run-id>
```

The helper must re-read both offers, publish both sequentially, add each listing to the dedicated General/CPS campaign at 10%, verify the live listing and campaign, and verify that this workflow did not enable Priority promotion. On any failure, execute the whole-pair rollback and report `publish_rolled_back` or `reconciliation_required`.

Write persistent history as `listed` only after both listing IDs and canonical URLs are independently returned and promotion verification passes. Never infer live identity from planned payloads.

## 6. Reconcile uncertainty

For an unknown API outcome, do not repeat the mutation. Run:

```bash
python3 scripts/ebay_listing.py reconcile --run-dir <run-directory>
```

Use the read-only observations to decide whether correction, rollback, or user input is needed. Preserve all run artifacts.

## 7. Accept a reviewed GitHub handoff

Use `.github/workflows/handoff.yml` for a schema-version-1 legacy prepared batch or a
schema-version-2 immediate-list batch from the installed `find-resale-products` skill.
Run `scripts/handoff_batch.py` to re-fetch both AliExpress products, enforce all gates,
resolve structured variants, and accept only validated HTTPS EPS media records. Block
only the affected product when current evidence, media, or variants are missing or
ambiguous.

Prepare with `ebay_listing.py prepare-independent`. Upload the complete unpublished run
as `prepared-<exact-run-id>` for seven days. Publish later only when the dispatch includes
the prepare Actions run ID and the matching exact logical run ID, using
`ebay_listing.py publish-independent`.

For schema version 2 with operation `list`, use the supplied EPS URLs directly without
re-importing them, prepare independently, then call `publish-independent` in the same
Action with the exact deterministic run ID. This immediate-list exception is authorized
only by the workflow's explicit `list` operation and confirmation input.

For this handoff path, preserve a successful sibling. If General/CPS promotion at 10%
fails after publication, withdraw only that product. Mark unknown mutations
`reconciliation_required` and use the read-only reconcile command instead of retrying.
Commit only verified successful listing history; never commit the dispatch payload or
prepared artifact.

## Unattended daily automation: draft first

The scheduled GitHub Actions run (`scripts/daily_run.py`) now follows the same
prepare-then-approve boundary as this skill instead of publishing unattended.

`daily_run.py --draft` sources two products (`--count 1`, or the `PRODUCTS_PER_RUN`
variable, asks for a single one), applies variant and AI enrichment, validates
each against eBay **read-only** (`ebay_listing.validate_for_draft`: Taxonomy category and
required item specifics only), and writes a draft to `state/drafts/<draft-id>.json` with
`status: draft`, `published: false`, `publish_allowed: false`. It creates nothing on eBay
— no EPS import, no inventory item, no offer. Each draft is mirrored into the workbook's
`Drafts` tab and rendered into an HTML review page that is emailed and uploaded as an
artifact.

`scripts/publish_drafts.py --live` is the separate approval step. It publishes only drafts
the user ticked `Publish? = YES` in the sheet, layering their edits onto the stored draft,
re-checking the AliExpress cost first, and calling `ebay_listing.list_one(..., enrich=False)`
so reviewed copy is never overwritten by the AI.

Do not infer Seller Hub draft availability here either. There is no public eBay API that
creates a Seller Hub draft, so the sheet and the draft JSON are the draft.

`daily_run.py --live` retains the original publish-immediately behaviour; the repository
variable `LISTING_MODE=auto` restores it for scheduled runs.

## References and helpers

- [references/ebay-api-setup.md](references/ebay-api-setup.md): Production developer, OAuth, Keychain, policies, location, and campaign setup.
- [references/ebay-api-workflow.md](references/ebay-api-workflow.md): category, media, inventory, offer, publish, promotion, and rollback sequence.
- [references/extension-backup.md](references/extension-backup.md): explicitly selected legacy extension workflow.
- [references/daily-sourcing.md](references/daily-sourcing.md): sourcing gates, niche, history, checkout, and evidence.
- [references/handoff-schema.md](references/handoff-schema.md): source, prepared, and live schemas.
- [references/report-format.md](references/report-format.md): unpublished review and live-result reporting.
- `scripts/ebay_setup.py`: secure Production OAuth and seller-account setup.
- `scripts/ebay_listing.py`: prepare, publish, rollback, and reconcile.
- `scripts/handoff_batch.py`: strict two-product dispatch validation and exact-variant mapping.
- `scripts/listing_job.py`: deterministic source and review validation.
- `scripts/extension_job.py`: legacy extension state validation for explicit fallback only.
- `scripts/candidate_ledger.py`, `daily_history.py`, `ebay_price.py`, `variant_rank.py`, and `run_budget.py`: retained deterministic helpers.
- `scripts/draft_store.py`, `draft_sheet.py`, `draft_preview.py`, and `publish_drafts.py`: the unattended draft-review-approve flow.
- `scripts/test_skill.py`: offline regression suite; it must never call Production.
- `scripts/test_drafts.py`: offline regression suite for the draft flow, including the
  "drafting mutates nothing on eBay" property.
