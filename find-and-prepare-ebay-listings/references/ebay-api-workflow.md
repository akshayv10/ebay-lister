# eBay API preparation and publication

## Contents

1. Preparation
2. Idempotency and reconciliation
3. Review boundary
4. Publication and promotion
5. Whole-pair rollback

## Preparation

Validate both `source.json` files before mutating eBay. Resolve the top EBAY_US Taxonomy category suggestion and fetch its aspect definitions. Require every aspect marked required; honor selection-only values. Confirm condition `NEW` through Metadata.

Import selected HTTPS images with Media API `createImageFromUrl`, then use returned EPS URLs. Continue past individual image failures but require at least one successful EPS image. Do not pass AliExpress URLs directly into the offer after import.

Use `ALI-<product-id>-<variant>-<hash>` deterministic SKUs. PUT every inventory item with quantity 1, verified product facts, aspects, and EPS images. For two or more variants, PUT one deterministic Inventory Item Group with the common facts, variant SKUs, option axes, and images.

Create one unpublished fixed-price offer per SKU with EBAY_US, GTC, USD price, category, `irvine-92618`, and the configured payment/return/free-shipping policy IDs. Omit per-offer listing description for grouped variations so the group description remains authoritative.

Read back inventory, group, and offers. Retrieve listing fees. Only then record `api_prepared`.

## Idempotency and reconciliation

Inventory-item and group PUT calls are full replacements and may be repeated only with the complete intended payload. Before POSTing an offer, query by deterministic SKU. Reuse and update exactly one unpublished match; block on duplicates or a published match.

After a timed-out POST, query by deterministic identity before retrying. If the outcome cannot be proven, record `reconciliation_required` and stop. Never run the extension as a recovery action.

## Review boundary

`review.md` must show title, source, category ID, each SKU/offer ID, prices, and EPS image count. It must clearly say nothing is live and report the exact run ID required for later approval. Do not claim Seller Hub draft availability.

## Publication and promotion

Require a later explicit user message plus the exact run ID. Re-read every offer and block if any is missing, changed, duplicated, or already published.

Publish single offers with `publishOffer`; publish grouped variants with `publishOfferByInventoryItemGroup`. Capture and re-read the listing ID. Immediately create a General/CPS ad in the dedicated campaign with bid percentage `10.0`, then verify the listing-to-campaign association and campaign funding settings. This workflow must never create a CPC campaign or Priority ad.

Update persistent history only after both listings and both ads pass. Record canonical `https://www.ebay.com/itm/<listing-id>` URLs.

## Whole-pair rollback

Treat both listings as one approval batch. On any publish, ad, or verification failure:

1. delete every ad created by this approval, in reverse order;
2. withdraw every single offer published by this approval, or withdraw the inventory group;
3. write `publish_rolled_back` only when every compensation succeeds;
4. write `reconciliation_required` when any rollback outcome is uncertain; and
5. never mark history as listed.
