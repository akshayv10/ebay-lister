# Daily sourcing contract

## Contents

1. Daily defaults
2. Niche rotation
3. Exclusion and branding
4. Candidate verification
5. History timing

## Daily defaults

- Find exactly two functionally distinct products from one niche.
- Use AliExpress for sourcing and eBay US only for duplicate inventory checks and preparing unpublished API offers. Do not research eBay sold listings, demand, or comparable sales.
- Allow unbranded products and lesser-known Chinese or non-global brands. Require US delivery region, rating at least 4.5, at least 25 reviews every run, at least 100 orders/sales, and a visible selected-variant single-unit item price of at least USD 15. Checkout cost is a downstream pricing input, not a viability gate.
- Reject ingestibles, medical claims, restricted goods, dangerous electrical products, counterfeits, franchise or licensed identities, and avoidable fitment dependence.
- Use `/Users/akballer47/Documents/Codex/resale-product-history.jsonl` and `Asia/Kolkata`.

## Niche rotation

Use this fixed cycle:

1. Smartphone Accessories
2. Hobbyist & Interactive Toys
3. Home Improvements & Lighting
4. Automotive Parts & Accessories
5. Beauty & Self Care

Before sourcing, inspect Seller Hub Active Listings. Ensure `Start date` is visible; add it through `Customize table` if necessary. Read accessible titles, product types, item numbers, links, and original Start dates. Ignore `Time left` because Good 'Til Canceled renewals do not reset the original listing age.

Convert PST/PDT Start dates to `Asia/Kolkata`. Reuse today's recorded niche on a same-day rerun. When the previous five local days are complete, advance from the most recent niche. When incomplete, choose the least recently used niche and break ties by cycle order. Use `scripts/daily_history.py` rather than choosing manually.

## Exclusion and branding

Build the exclusion set from accessible active eBay listings and every history record. Reject exact URLs and IDs, the same functional product under another title, cosmetic relists, recreated listings, and finalists that are close substitutes.

Reject established global consumer brands and products that create counterfeit, trademark, franchise, celebrity, sports-team, or licensed-identity risk. Examples of brands to reject include Apple, Samsung, Lenovo, Sony, Google, Microsoft, Bose, JBL, Nike, Adidas, and similarly established international brands. This list is illustrative, not exhaustive.

Do not reject an unfamiliar, lesser-known, or Chinese brand merely because a name or logo appears. Preserve the verified brand in the eBay Brand field. Store names do not automatically brand a product. Compatibility wording is acceptable when it truthfully describes fit and does not misrepresent the product as made by the compatible brand.

## Candidate verification

Use this order so rejected candidates are inexpensive:

1. Prefilter a batch of search cards without opening obviously ineligible products.
2. On the product page, verify rating, at least 25 reviews, at least 100 sales, US availability, and allowed brand classification using only the compact top product panel plus specifications when necessary.
3. Check the once-per-run inventory/history exclusion set.
4. Select the exact single-unit variant and verify its ordinary visible item price is at least USD 15, excluding bulk tiers, coupons, coins, credits, and rewards.
5. When the visible price passes, record the candidate as accepted at gate `visible_price`.
6. Click Buy Now only to capture a positive delivered cost for deterministic eBay pricing and record that evidence at gate `checkout_pricing`.
7. Only when checkout pricing is available, write the complete API `source.json`. Do not mutate eBay until two products qualify and both source records validate.

Stop at the first failed gate. Do not read the full page or open checkout merely to gather evidence for a candidate that has already failed.

For every finalist capture:

- canonical product URL and ID;
- title, niche, and functional fingerprint;
- rating, review count, order/sales count, and visible US region;
- title, brand field, packaging, images, and selected-combination evidence showing the product is unbranded, an allowed lesser-known brand, or a prohibited global brand;
- every real option axis and available combination;
- visible single-unit page price and Buy Now checkout breakdown/final delivered cost for each selected combination;
- material risk. Do not add an eBay-demand or sold-comparable rationale.

Select a combination and record its ordinary visible single-unit item price first. If it is below USD 15, reject without clicking Buy Now. Once it passes, the product is viable regardless of checkout amount. Then click Buy Now only to reach read-only checkout review and record the positive delivered cost after automatic discounts, shipping, taxes, and import charges for eBay pricing. Do not apply coupons, coins, credits, rewards, or change address/payment settings. Never click a purchase control.

Reject and replace when required viability evidence is missing, the visible price is below USD 15, an established global brand or counterfeit/trademark risk is present, or inventory/history overlaps. Do not reject because checkout is blocked or because its total is below USD 15. If checkout cannot provide a positive delivered cost, preserve the accepted candidate and pause before `source.json` initialization with a `checkout_pricing` blocker. Do not reject solely because a lesser-known brand is present. Never relax a threshold or switch niches to fill the second slot. Never treat an exhausted query, search page, or initial set of subcategories as a sourcing failure: keep broadening queries and exploring functionally relevant subcategories within the assigned niche until two products qualify. Pause only for an explicit external blocker named in the main skill contract, preserving the run for resumption.

Maintain `candidate-ledger.jsonl` through `scripts/candidate_ledger.py`. Record every accepted or rejected candidate with its first terminal gate and every search-result batch with a stable key and productive/exhausted outcome. Use it to skip every previously evaluated URL and batch. Reuse one candidate tab, scan cards in batches, and move forward after an unproductive page or query; persistence must broaden the search rather than repeat it.

Capture verified media, factual copy inputs, Brand, and item specifics for the API payload. Reject the product if source media reveals an established global brand, counterfeit risk, a material contradiction, or another stated exclusion. Generate no claim that is unsupported by the source evidence.

## History timing

Do not write failed candidates. Do not mark an unpublished API offer as a draft or listing in persistent history. Write listing fields only after the separately approved publication returns and independently verifies both live listings.
