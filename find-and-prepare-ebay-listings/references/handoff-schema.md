# API listing schemas

## Source

Each product directory contains `source.json` with required sourcing identity, verified listing content, and 1–4 combinations:

```json
{
  "run_id": "20260721T120000-product-1005000000000000",
  "local_calendar_date": "2026-07-21",
  "assigned_niche": "Smartphone Accessories",
  "product_id": "1005000000000000",
  "aliexpress_url": "https://www.aliexpress.us/item/1005000000000000.html",
  "source_title": "Verified source title",
  "functional_fingerprint": "normalized function",
  "verified_brand": "Unbranded",
  "listing_title": "Factual eBay title, 80 characters maximum",
  "listing_description": "Factual description supported by source evidence.",
  "condition": "NEW",
  "category_query": "concise product type",
  "aspects": {"Brand": ["Unbranded"], "Type": ["Verified type"]},
  "source_images": ["https://verified-source.example/image.jpg"],
  "selected_variants": [{
    "id": "black-usb-c",
    "options": {"Color": "Black", "Connector": "USB-C"},
    "visible_item_price": "17.25",
    "delivered_total": "18.40",
    "quantity": 1
  }]
}
```

Use decimal strings for money. Brand, aspects, copy, images, and combinations must come from verified evidence. `listing_job.py init` adds deterministic SKUs and prices.

## Prepared result

Preparation records `status: api_prepared`, `published: false`, and `publish_allowed: false`, plus:

- Production/EBAY_US and location key `irvine-92618`;
- selected policy and General campaign IDs;
- category and normalized required aspects;
- EPS image URLs and nonfatal image-import failures;
- inventory SKU readbacks and optional group readback;
- unpublished offer IDs/readbacks; and
- listing-fee response.

Never store credentials, tokens, callback codes, or the full address.

## Live result

Only the separately approved publish command may add `status: live`, `published: true`, listing IDs, canonical URLs, General campaign/ad IDs, bid `10.0`, and `priority_promotion_enabled: false`.

Any publish failure produces `publish_rolled_back` or `reconciliation_required`, never `live`.
