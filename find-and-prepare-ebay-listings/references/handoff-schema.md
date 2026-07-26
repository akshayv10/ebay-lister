# API listing schemas

## External reviewed batch

The `find-resale-products` workflow dispatch accepts schema version 1 for legacy
reviewed preparation and schema version 2 for immediate listing. Both contain
`local_calendar_date`, `assigned_niche`, and exactly two distinct products. Each product
must provide canonical AliExpress identity, title, functional fingerprint, verified
brand plus evidence, rating/reviews/orders, explicit US-region confirmation, material
risk, and:

```json
{
  "selected_variant": {
    "options": {"Color": "Black", "Connector": "USB-C"},
    "display_label": "Black / USB-C, quantity 1",
    "visible_item_price": "17.99",
    "checkout_total": "Unavailable"
  }
}
```

Schema version 2 additionally requires a `media` object with 1–24 ordered records. Each
record has a unique SHA-256, EPS image ID, HTTPS EPS URL, role, positive unique order,
and optional structured `variant_options`. Include the ordered image hashes in the
deterministic batch ID. Re-fetch current AliExpress detail and never replace an
unresolved selection with the cheapest or default variant.

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

Only the separately approved legacy publish command or an explicit schema-version-2
`list` operation may add `status: live`, `published: true`, listing IDs, canonical URLs,
General campaign/ad IDs, bid `10.0`, and `priority_promotion_enabled: false`.

Any publish failure produces `publish_rolled_back` or `reconciliation_required`, never `live`.
