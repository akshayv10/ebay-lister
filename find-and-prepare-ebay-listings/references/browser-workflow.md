# Chrome extension handoff and final-form audit

## Contents

1. Browser budget
2. Button contract
3. State handling
4. eBay tab binding
5. Media settling
6. Final audit

## Browser budget

Read Chrome documentation once and retain the binding. Count every browser call, including failures. Hard-stop at 60 calls, 12 DOM snapshots, or three browser timeouts. Prefer targeted DOM reads and page-side waits.

Source both products before starting either extension run. Process the extension and eBay form sequentially: product one must reach `ready_for_user` before product two starts.

## Button contract

The installed extension exposes this public page API on an AliExpress product page:

```js
const button = document.querySelector('#mla-ebay-button');
const state = button?.dataset.mlaState;
const message = button?.dataset.mlaMessage || '';
```

Authorized click:

```js
document.querySelector('#mla-ebay-button').click();
```

Click exactly once only after confirming that the button exists, the current URL is the accepted canonical product, and the initial state is `idle`. Record the source tab ID, click time, and pre-click Chrome tab/window IDs.

Use a page-side `MutationObserver` for `data-mla-state` and `data-mla-message` when the browser surface supports an asynchronous evaluation. Resolve only on `done`, `error`, or an eight-minute deadline. If browser evaluation itself times out, count it and inspect the existing button without clicking again.

## State handling

| State | Meaning | Action |
| --- | --- | --- |
| `idle` | Ready before a run | Click once. After a recorded click, an unexpected return to `idle` is a failure. |
| `running` | This tab's extension run is active | Wait. Never click. |
| `busy` | Another tab owns the global extension workflow | Wait before any click. If observed after this tab's click, stop for ownership loss. |
| `done` | Extension handoff finished | Accept only when observed after this run's recorded click. Continue to eBay verification. |
| `error` | Extension failed | Record the message, preserve tabs, and stop without retrying. |

A pre-click `done` or `error` is stale. Reload the same canonical product page once and require `idle`; if it remains terminal, stop. A missing button is an extension-availability blocker. Unknown or blank states are blockers, not permission to click.

Never re-click after this run records a click, even after error, timeout, missing eBay tab, or an incomplete form. This prevents duplicate eBay forms.

## eBay tab binding

Before clicking, record every Chrome tab and window ID. After `done`, compare the current set and select only a newly created tab whose URL is HTTPS on `ebay.com` or a subdomain and whose page is an eBay prelisting/listing form. The extension currently opens this tab in a separate unfocused window.

Reject ambiguous handoffs: no new eBay form, more than one plausible new form, a reused source tab, or an eBay tab already paired with another product. Preserve ambiguous tabs for diagnosis. Record the source tab ID, eBay tab ID, form URL, terminal state `done`, and terminal message with `listing_job.py record-extension`.

## Media settling

Extension state `done` can occur while its asynchronous main-image upload is still finishing. Before auditing:

1. Read the extension media panel's targeted status when present.
2. Wait until it no longer reports preparing, downloading, uploading, or auto-uploading.
3. Confirm at least one image appears in eBay's live photo area and the count is stable across two targeted reads separated by a UI-settle wait.
4. Require zero visible extension upload failures. Add accurate optional media only when needed; never duplicate an existing upload.

Set `media_settled: true` only after all four conditions pass. A hidden or absent panel is acceptable only when the live eBay photo area itself proves a stable nonzero image count and no failure indicator is visible.

## Final audit

Compare the live form with `source.json` and the deterministic expected prices. Verify and correct:

- nonblank title no longer than 80 characters;
- a live eBay category, condition, and factual nonblank description;
- stable media with at least one image and no upload failures;
- quantity exactly 1 and variation IDs exactly matching the selected source combinations;
- verified product Brand, using `Unbranded` only when the source evidence is unbranded;
- every required item specific shown by the chosen category;
- every variation price exactly matching its expected deterministic price;
- nonblank item location, free buyer shipping, and existing account policies;
- Promoted Listings General on at exactly 10% and Priority promotion off;
- no unfinished required field and no save or publish action.

Use the standardized controlled-field sequence: focus, select all, type once, commit/blur, wait for formatting, then read the smallest live section. Do not try multiple synthetic DOM-event variants.

Write these observations to `final-audit.json` using [handoff-schema.md](handoff-schema.md). A blank or mismatched field is a failed checkpoint. Correct the same page only. Three identical failures block the result and stop the complete run.
