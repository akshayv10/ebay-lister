#!/usr/bin/env python3
"""Offline tests for the draft-then-publish flow.

Covers the draft store, batch selection, parking drafts eBay keeps refusing, the
stale-cost guard, the sheet encodings, and the central safety property: drafting must
not mutate anything on eBay.

No network, no eBay, no Google Sheets.
"""

from __future__ import annotations

import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import draft_preview
import draft_sheet
import draft_store
import listing_job
import notify
import publish_drafts
from listing_job import JobError, normalize_source

SCRIPTS = Path(__file__).parent


def sample_source(**overrides: Any) -> dict[str, Any]:
    source = {
        "run_id": "20260727T033012-product-1005006000000123",
        "local_calendar_date": "2026-07-27",
        "assigned_niche": "hobby",
        "product_id": "1005006000000123",
        "aliexpress_url": "https://www.aliexpress.us/item/1005006000000123.html",
        "source_title": "RC Stunt Car 4WD Remote Control Drift Buggy",
        "functional_fingerprint": "rc stunt car",
        "verified_brand": "Unbranded",
        "listing_title": "RC Stunt Car 4WD Remote Control Drift Buggy",
        "listing_description": "<p>Brand new RC car.</p>",
        "condition": "NEW",
        "category_query": "RC Stunt Car",
        "aspects": {"Brand": ["Unbranded"], "MPN": ["N/A"], "Type": ["Buggy"]},
        "source_images": ["https://ae01.alicdn.com/kf/a.jpg", "https://ae01.alicdn.com/kf/b.jpg"],
        "selected_variants": [
            {
                "id": "default",
                "options": {},
                "visible_item_price": "19.99",
                "delivered_total": "24.50",
                "quantity": 1,
            }
        ],
    }
    source.update(overrides)
    return source


def sample_draft(directory: Path) -> dict[str, Any]:
    draft = draft_store.new_draft(
        normalize_source(sample_source()),
        run_stamp="20260727T033012",
        validation={"category_id": "182182", "normalized_aspects": {}, "warnings": []},
        spare_images=["https://ae01.alicdn.com/kf/spare.jpg"],
    )
    draft_store.save(draft, directory)
    return draft


# ------------------------------------------------------------------- draft store

def test_draft_id_is_deterministic_per_run_and_product() -> None:
    first = draft_store.make_draft_id("20260727T033012", "1005006000000123")
    second = draft_store.make_draft_id("20260727T033012", "1005006000000123")
    assert first == second == "20260727T033012-1005006000000123"


def test_draft_round_trips_through_disk() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        loaded = draft_store.load(draft["draft_id"], directory)
        assert loaded["source"]["listing_title"] == draft["source"]["listing_title"]
        assert loaded["status"] == "draft"
        assert loaded["published"] is False
        assert loaded["publish_allowed"] is False
        assert draft_store.is_publishable(loaded)


def test_live_draft_is_never_publishable_again() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        draft_store.mark(draft, draft_store.STATUS_LIVE, directory=directory, listing_id="999")
        reloaded = draft_store.load(draft["draft_id"], directory)
        assert reloaded["published"] is True
        assert not draft_store.is_publishable(reloaded)
        assert [item["draft_id"] for item in draft_store.pending(directory)] == []


def test_failed_and_blocked_drafts_stay_publishable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        for status in (draft_store.STATUS_BLOCKED, draft_store.STATUS_PUBLISH_FAILED):
            draft_store.mark(draft, status, directory=directory, publish_error="nope")
            assert draft_store.is_publishable(draft_store.load(draft["draft_id"], directory))


# ------------------------------------------------------------- sheet encodings

def test_aspects_round_trip() -> None:
    aspects = {"Brand": ["Unbranded"], "Color": ["Red", "Blue"], "MPN": ["N/A"]}
    assert draft_sheet.parse_aspects(draft_sheet.encode_aspects(aspects)) == aspects


def test_images_round_trip_and_accept_any_separator() -> None:
    urls = ["https://x/a.jpg", "https://x/b.jpg"]
    assert draft_sheet.parse_images(draft_sheet.encode_images(urls)) == urls
    assert draft_sheet.parse_images("https://x/a.jpg, https://x/b.jpg") == urls
    assert draft_sheet.parse_images("https://x/a.jpg\n\nhttps://x/b.jpg  ") == urls
    # A duplicate paste must not produce a duplicate image.
    assert draft_sheet.parse_images("https://x/a.jpg https://x/a.jpg") == ["https://x/a.jpg"]


def test_variants_round_trip_and_preserve_variation_image() -> None:
    variants = [
        {"id": "red", "options": {"Color": "Red"}, "visible_item_price": "19.99",
         "delivered_total": "24.50", "quantity": 1, "image": "https://x/red.jpg"},
        {"id": "blue", "options": {"Color": "Blue"}, "visible_item_price": "21.00",
         "delivered_total": "25.75", "quantity": 1, "image": "https://x/blue.jpg"},
    ]
    parsed = draft_sheet.parse_variants(draft_sheet.encode_variants(variants), variants)
    assert [item["id"] for item in parsed] == ["red", "blue"]
    assert parsed[0]["options"] == {"Color": "Red"}
    assert parsed[1]["delivered_total"] == "25.75"
    # The cell has no image column, so the stored per-variation photo must survive.
    assert parsed[0]["image"] == "https://x/red.jpg"




def test_draft_row_matches_header_width_and_key_column() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        draft = sample_draft(Path(tmp))
        row = draft_sheet.draft_row(draft)
        assert len(row) == len(draft_sheet.HEADERS)
        assert row[0] == draft["draft_id"]
        assert row[draft_sheet.COL["Thumbnail"]].startswith('=IMAGE("https://')
        assert row[draft_sheet.COL["Spare Images"]] == "https://ae01.alicdn.com/kf/spare.jpg"


# ------------------------------------------------------------------ reviewer edits

def _row_for(draft: dict[str, Any], **cells: Any) -> list[Any]:
    row = draft_sheet.draft_row(draft)
    for name, value in cells.items():
        row[draft_sheet.COL[name]] = value
    return row



# -------------------------------------------------------------------- price override

def test_price_override_replaces_the_computed_price() -> None:
    source = sample_source()
    source["selected_variants"][0]["price_override"] = "79.99"
    normalized = normalize_source(source)
    assert normalized["selected_variants"][0]["expected_ebay_price"] == "79.99"


def test_price_without_override_still_comes_from_the_quote() -> None:
    normalized = normalize_source(sample_source())
    priced = normalized["selected_variants"][0]
    assert priced["expected_ebay_price"] != priced["delivered_total"]
    assert Decimal(priced["expected_ebay_price"]) > Decimal(priced["delivered_total"])


def test_price_override_at_or_below_cost_is_rejected() -> None:
    for bad in ("24.50", "10.00"):
        source = sample_source()
        source["selected_variants"][0]["price_override"] = bad
        try:
            normalize_source(source)
        except JobError as exc:
            assert "delivered cost" in str(exc)
        else:  # pragma: no cover - the guard must fire
            raise AssertionError(f"override {bad} should have been rejected")



# ------------------------------------------------------------------ category override

def test_category_override_must_be_numeric() -> None:
    source = sample_source(category_id_override="not-a-category")
    try:
        normalize_source(source)
    except JobError as exc:
        assert "numeric" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a non-numeric category override should have been rejected")


# ------------------------------------------------------------------- stale drafts

class _FakeAli:
    """Stands in for the AliExpress module inside cost_check."""

    def __init__(self, price: str | None, raises: bool = False):
        self.price = Decimal(price) if price is not None else None
        self.raises = raises

    def get_product_detail(self, product_id: str) -> dict[str, Any]:
        if self.raises:
            raise RuntimeError("gateway timeout")
        return {}

    def flatten_detail(self, detail: dict[str, Any]) -> dict[str, Any]:
        return {"id": "1005006000000123", "price": self.price, "sku_id": ""}

    def freight(self, product_id: str, sku_id: str, price: Decimal) -> Decimal:
        return Decimal("4.51")

    def delivered_total(self, price: Decimal, shipping: Decimal | None) -> Decimal:
        return price + (shipping or Decimal("0"))


def _with_fake_ali(fake: _FakeAli, source: dict[str, Any]):
    original = publish_drafts.ali_api
    publish_drafts.ali_api = fake  # type: ignore[assignment]
    try:
        return publish_drafts.cost_check(source)
    finally:
        publish_drafts.ali_api = original


def test_cost_check_passes_when_the_price_is_stable() -> None:
    ok, message, _ = _with_fake_ali(_FakeAli("19.99"), normalize_source(sample_source()))
    assert ok
    assert message == ""


def test_cost_check_blocks_a_draft_whose_cost_jumped() -> None:
    # 19.99 + 4.51 = 24.50 at draft time; 30.00 + 4.51 = 34.51 is +40.9%.
    ok, message, _ = _with_fake_ali(_FakeAli("30.00"), normalize_source(sample_source()))
    assert not ok
    assert "rose" in message and "%" in message


def test_cost_check_repriced_when_the_cost_fell() -> None:
    source = normalize_source(sample_source())
    ok, message, refreshed = _with_fake_ali(_FakeAli("14.99"), source)
    assert ok
    assert "moved" in message
    assert refreshed["selected_variants"][0]["delivered_total"] == "19.50"


def test_cost_check_blocks_an_unavailable_product() -> None:
    ok, message, _ = _with_fake_ali(_FakeAli(None), normalize_source(sample_source()))
    assert not ok
    assert "no longer available" in message


def test_cost_check_survives_a_lookup_failure() -> None:
    """A transient AliExpress error must not silently drop an approved listing."""
    ok, message, _ = _with_fake_ali(_FakeAli("19.99", raises=True), normalize_source(sample_source()))
    assert ok
    assert "Could not re-check" in message


def test_a_hand_set_price_survives_a_cost_refresh() -> None:
    """The refresh updates the cost but must not overwrite the reviewer's price."""
    source = normalize_source(sample_source())
    source["selected_variants"][0]["price_override"] = "88.00"
    ok, _, refreshed = _with_fake_ali(_FakeAli("14.99"), source)
    assert ok
    variant = refreshed["selected_variants"][0]
    # Cost is re-read (14.99 + 4.51 freight) so the override is checked against it...
    assert variant["delivered_total"] == "19.50"
    # ...but the price the reviewer chose is untouched.
    assert variant["price_override"] == "88.00"
    assert normalize_source(refreshed)["selected_variants"][0]["expected_ebay_price"] == "88.00"


# --------------------------------------------------------------- safety properties

def test_drafting_performs_no_mutating_ebay_calls() -> None:
    """validate_for_draft may only read. Any POST/PUT would create eBay-side state that
    the reviewer cannot see and that would go stale the moment they edit the draft."""
    import ebay_listing

    calls: list[tuple[str, str]] = []

    class ReadOnlyClient:
        def request(self, method, path, query=None, json_body=None, expected=None):
            calls.append((method, path))
            if method.upper() != "GET":
                raise AssertionError(f"draft validation attempted a mutating call: {method} {path}")
            if "get_default_category_tree_id" in path:
                return type("R", (), {"data": {"categoryTreeId": "0"}})()
            if "get_category_suggestions" in path:
                return type("R", (), {"data": {"categorySuggestions": [{"category": {"categoryId": "182182"}}]}})()
            if "get_item_aspects_for_category" in path:
                return type("R", (), {"data": {"aspects": [
                    {"localizedAspectName": "Brand", "aspectConstraint": {"aspectRequired": True}},
                    {"localizedAspectName": "Type", "aspectConstraint": {"aspectRequired": False}},
                ]}})()
            return type("R", (), {"data": {}})()

    os.environ.pop("DRAFT_VERIFY_IMAGES", None)
    outcome = ebay_listing.validate_for_draft(ReadOnlyClient(), normalize_source(sample_source()))
    assert outcome["category_id"] == "182182"
    assert all(method == "GET" for method, _ in calls)
    assert calls, "validation should have read the taxonomy"


def test_validate_for_draft_reports_failures_instead_of_raising() -> None:
    import ebay_listing
    from ebay_common import EbayError

    class BrokenClient:
        def request(self, *args, **kwargs):
            raise EbayError("taxonomy is down")

    outcome = ebay_listing.validate_for_draft(BrokenClient(), normalize_source(sample_source()))
    assert outcome["ok"] is False
    assert outcome["category_id"] == ""
    assert any("taxonomy is down" in warning for warning in outcome["warnings"])


def test_publishing_a_draft_disables_enrichment() -> None:
    """The reviewed copy must reach eBay verbatim — publish_drafts must never let the AI
    rewrite a title the reviewer just fixed."""
    source = (SCRIPTS / "publish_drafts.py").read_text(encoding="utf-8")
    assert "enrich=False" in source, "publish must disable enrichment"

    listing = (SCRIPTS / "ebay_listing.py").read_text(encoding="utf-8")
    assert "if enrich:" in listing, "list_one must gate enrichment behind the flag"


def test_daily_run_draft_mode_never_publishes() -> None:
    source = (SCRIPTS / "daily_run.py").read_text(encoding="utf-8")
    draft_index = source.index('if mode == "draft":')
    import_index = source.index("from ebay_listing import list_resilient")
    assert draft_index < import_index, "the draft early return must precede any publish call"


def test_publish_workflow_is_one_press() -> None:
    """The whole point: Run workflow publishes, with no ticking or extra choices."""
    workflow = (SCRIPTS.parent.parent / ".github" / "workflows" / "publish-drafts.yml").read_text(
        encoding="utf-8"
    )
    assert "default: publish" in workflow, "running the workflow must publish"
    assert "dry-run" in workflow, "a validate-only mode must stay available"
    assert "default: latest" in workflow, "the default scope is the newest batch"
    assert "--live" in workflow and "--all" in workflow


def test_daily_workflow_defaults_to_draft_and_can_revert_to_auto() -> None:
    workflow = (SCRIPTS.parent.parent / ".github" / "workflows" / "daily.yml").read_text(
        encoding="utf-8"
    )
    block = workflow.split("mode:", 1)[1][:500]
    assert "default: draft" in block, "the daily workflow must default to drafting"
    assert "vars.LISTING_MODE == 'auto'" in workflow, "there must be a documented way back to auto"
    assert "--draft" in workflow and "--live" in workflow


# -------------------------------------------------------------- publish integration

class _StubListOne:
    """Captures what publish_one hands to ebay_listing.list_one."""

    def __init__(self, fail: str = ""):
        self.fail = fail
        self.seen: dict[str, Any] = {}

    def __call__(self, client, config, source_path, enrich=True):
        from ebay_common import EbayError, read_json

        self.seen = {"source": read_json(source_path), "enrich": enrich}
        if self.fail:
            raise EbayError(self.fail)
        product = dict(self.seen["source"])
        product.update({
            "status": "live", "published": True,
            "listing_id": "1122334455",
            "ebay_url": "https://www.ebay.com/itm/1122334455",
        })
        return product


def _publish(draft, stub, directory):
    import ebay_listing

    original_list_one = ebay_listing.list_one
    original_dir = draft_store.DRAFT_DIR
    original_ali = publish_drafts.ali_api
    ebay_listing.list_one = stub
    draft_store.DRAFT_DIR = directory
    publish_drafts.ali_api = _FakeAli("19.99")
    try:
        with tempfile.TemporaryDirectory() as runs:
            return publish_drafts.publish_one(draft, object(), {}, Path(runs))
    finally:
        ebay_listing.list_one = original_list_one
        draft_store.DRAFT_DIR = original_dir
        publish_drafts.ali_api = original_ali




def test_a_failed_publish_leaves_the_draft_retryable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        stub = _StubListOne(fail="eBay rejected the category")
        outcome = _publish(draft, stub, directory)

        assert outcome["status"] == "publish_failed"
        stored = draft_store.load(draft["draft_id"], directory)
        assert stored["published"] is False
        assert "eBay rejected the category" in stored["publish_error"]
        assert "attempt 1 of 2" in stored["publish_error"]
        assert draft_store.is_publishable(stored), "a failed draft must be fixable and re-approved"




def test_a_stale_draft_is_blocked_before_ebay_is_touched() -> None:
    import ebay_listing

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        stub = _StubListOne()
        original_list_one, original_dir = ebay_listing.list_one, draft_store.DRAFT_DIR
        original_ali = publish_drafts.ali_api
        ebay_listing.list_one = stub
        draft_store.DRAFT_DIR = directory
        publish_drafts.ali_api = _FakeAli("30.00")  # +40.9% delivered cost
        try:
            with tempfile.TemporaryDirectory() as runs:
                outcome = publish_drafts.publish_one(draft, object(), {}, Path(runs))
        finally:
            ebay_listing.list_one = original_list_one
            draft_store.DRAFT_DIR = original_dir
            publish_drafts.ali_api = original_ali

        assert outcome["status"] == "blocked"
        assert stub.seen == {}, "a stale draft must never reach eBay"
        assert draft_store.is_publishable(draft_store.load(draft["draft_id"], directory))


# ------------------------------------------------ regressions from PR #12 review

def test_a_price_override_is_validated_against_todays_cost() -> None:
    """A draft priced when the item was cheap must not list below cost after it rises.

    Cost 100.00 -> 109.00 is inside the 10% drift limit, so the draft is still allowed;
    but an override of 105.00 is now below cost and must be rejected, not listed.
    """
    source = sample_source()
    source["selected_variants"][0].update({
        "visible_item_price": "95.49", "delivered_total": "100.00", "price_override": "105.00",
    })
    source = normalize_source(source)

    ok, _, refreshed = _with_fake_ali(_FakeAli("104.49"), source)  # +4.51 freight = 109.00
    assert ok, "9% drift is within the limit, so the draft itself is not blocked"
    assert refreshed["selected_variants"][0]["delivered_total"] == "109.00", \
        "the delivered cost must be refreshed even when an override is set"
    try:
        normalize_source(refreshed)
    except JobError as exc:
        assert "delivered cost" in str(exc)
    else:  # pragma: no cover - this is the money-losing path
        raise AssertionError("a now-below-cost override must be rejected")


def test_multi_variant_costs_are_rechecked_per_variation() -> None:
    class _FakeVariantAli(_FakeAli):
        def __init__(self, records):
            super().__init__("19.99")
            self.records = records

        def variant_records(self, product_id):
            return "Color", self.records

    source = sample_source()
    source["selected_variants"] = [
        {"id": "red", "options": {"Color": "Red"}, "visible_item_price": "19.99",
         "delivered_total": "24.50", "quantity": 1},
        {"id": "blue", "options": {"Color": "Blue"}, "visible_item_price": "21.00",
         "delivered_total": "25.75", "quantity": 1},
    ]
    source = normalize_source(source)

    # One variation's cost jumped well past the limit.
    ok, message, _ = _with_fake_ali(_FakeVariantAli([
        {"id": "red", "visible_item_price": "19.99", "delivered_total": "24.50"},
        {"id": "blue", "visible_item_price": "40.00", "delivered_total": "44.00"},
    ]), source)
    assert not ok
    assert "blue" in message and "rose" in message

    # A variation that disappeared is also a blocker, not a silent listing.
    ok, message, _ = _with_fake_ali(_FakeVariantAli([
        {"id": "red", "visible_item_price": "19.99", "delivered_total": "24.50"},
    ]), normalize_source(sample_source(selected_variants=[
        {"id": "red", "options": {"Color": "Red"}, "visible_item_price": "19.99",
         "delivered_total": "24.50", "quantity": 1},
        {"id": "blue", "options": {"Color": "Blue"}, "visible_item_price": "21.00",
         "delivered_total": "25.75", "quantity": 1},
    ])))
    assert not ok
    assert "no longer offered" in message






def test_drafted_products_are_excluded_from_the_next_sourcing_run() -> None:
    """Otherwise the same product is drafted again tomorrow and listed twice."""
    from daily_history import same_record

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        views = draft_store.history_views(directory)
        assert views, "a saved draft must produce an exclusion view"

        # The view must match the same candidate that sourcing would build tomorrow.
        candidate = {
            "aliexpress_url": draft["source"]["aliexpress_url"],
            "functional_fingerprint": draft["source"]["functional_fingerprint"],
            "product_title": draft["source"]["source_title"],
        }
        assert any(same_record(view, candidate) for view in views)

        unrelated = {
            "aliexpress_url": "https://www.aliexpress.us/item/1005009999999999.html",
            "functional_fingerprint": "garden hose reel",
            "product_title": "Garden Hose Reel Wall Mounted",
        }
        assert not any(same_record(view, unrelated) for view in views)


def test_daily_run_feeds_drafts_into_the_sourcing_exclusion_set() -> None:
    source = (SCRIPTS / "daily_run.py").read_text(encoding="utf-8")
    assert "draft_store.history_views()" in source
    assert "sourcing_history" in source
    assert "local_date, sourcing_history" in source, "source_products must get the augmented set"
    assert "niche_priority(history," in source, "niche rotation must still use real history only"


def test_state_committing_workflows_share_a_concurrency_group() -> None:
    """Overlapping state/ pushes can resurrect an already-published draft."""
    workflows = SCRIPTS.parent.parent / ".github" / "workflows"
    daily = (workflows / "daily.yml").read_text(encoding="utf-8")
    publish = (workflows / "publish-drafts.yml").read_text(encoding="utf-8")
    assert "group: ebay-lister-state" in daily
    assert "group: ebay-lister-state" in publish
    for text in (daily, publish):
        assert "git pull --rebase origin" in text, "a rejected push must be retried"
        assert "::error::" in text, "an unrecoverable push must fail the job, not pass silently"


# ------------------------------------------------------------------------ rendering

def test_preview_renders_and_escapes_untrusted_text() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        draft = sample_draft(Path(tmp))
        draft["source"]["listing_title"] = 'Evil <script>alert("x")</script> Title'
        draft["source"]["listing_description"] = "<p>Fine</p><script>alert('no')</script>"
        html = draft_preview.render([draft], sheet_url="https://sheets.example/x", date="2026-07-27")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html          # the title was escaped
        assert "<p>Fine</p>" in html             # allowed description markup survived
        assert "https://sheets.example/x" in html
        assert "Nothing is live" in html


def test_drafts_email_states_nothing_is_live_and_how_to_publish() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        draft = sample_draft(Path(tmp))
        subject, text, html = notify.compose_drafts({
            "date": "2026-07-27",
            "niche": "hobby",
            "drafts": [draft],
            "draft_count": 1,
            "preview_html": draft_preview.render([draft]),
            "sheet_url": "https://sheets.example/x",
        })
        assert "1 ready to review" in subject
        assert "NOTHING IS LIVE" in text
        assert draft["draft_id"] in text
        assert "Run workflow" in text
        assert "https://sheets.example/x" in text
        assert "<html>" in html


# ------------------------------------------------- batch selection and the backlog

def _batch(directory: Path, run_stamp: str, product_id: str, **fields: Any) -> dict[str, Any]:
    """Save one draft belonging to a named batch."""
    source = normalize_source(sample_source(
        product_id=product_id,
        aliexpress_url=f"https://www.aliexpress.us/item/{product_id}.html",
        run_id=f"{run_stamp}-product-{product_id}",
    ))
    draft = draft_store.new_draft(source, run_stamp=run_stamp)
    draft.update(fields)
    draft_store.save(draft, directory)
    return draft


def _with_draft_dir(directory: Path, function, *args, **kwargs):
    original = draft_store.DRAFT_DIR
    draft_store.DRAFT_DIR = directory
    try:
        return function(*args, **kwargs)
    finally:
        draft_store.DRAFT_DIR = original


def test_only_the_newest_batch_is_selected() -> None:
    """One press should publish today's two listings, not a week's accumulation."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _batch(directory, "20260727T212942", "1005006000000001")
        _batch(directory, "20260727T212942", "1005006000000002")
        _batch(directory, "20260728T114005", "1005006000000003")

        selected, batch, _ = _with_draft_dir(directory, publish_drafts.select_drafts)
        assert batch == "20260728T114005"
        assert [d["product_id"] for d in selected] == ["1005006000000003"]


def test_older_drafts_are_reported_not_swept_up() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _batch(directory, "20260727T212942", "1005006000000001")
        _batch(directory, "20260727T212942", "1005006000000002")
        _batch(directory, "20260728T114005", "1005006000000003")

        backlog = draft_store.backlog(directory, exclude_run_stamp="20260728T114005")
        assert len(backlog) == 2
        assert {d["run_stamp"] for d in backlog} == {"20260727T212942"}


def test_all_publishes_the_whole_backlog() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _batch(directory, "20260727T212942", "1005006000000001")
        _batch(directory, "20260728T114005", "1005006000000003")
        selected, batch, _ = _with_draft_dir(
            directory, publish_drafts.select_drafts, publish_all=True
        )
        assert batch == "all"
        assert len(selected) == 2


def test_a_named_batch_can_be_published() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _batch(directory, "20260727T212942", "1005006000000001")
        _batch(directory, "20260728T114005", "1005006000000003")
        selected, batch, _ = _with_draft_dir(
            directory, publish_drafts.select_drafts, run_stamp="20260727T212942"
        )
        assert batch == "20260727T212942"
        assert [d["product_id"] for d in selected] == ["1005006000000001"]


def test_live_drafts_are_never_reselected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = _batch(directory, "20260728T114005", "1005006000000003")
        draft_store.mark(draft, draft_store.STATUS_LIVE, directory=directory, listing_id="1")
        selected, batch, _ = _with_draft_dir(directory, publish_drafts.select_drafts)
        assert selected == []
        assert batch == ""


# ------------------------------------------------------------------------ parking

def test_a_draft_ebay_keeps_refusing_is_parked() -> None:
    """The real case: a branded medical device eBay will never accept must stop being
    retried on every press."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = _batch(directory, "20260728T114005", "1005006000000003",
                       status=draft_store.STATUS_PUBLISH_FAILED, publish_attempts=1)
        draft_store.save(draft, directory)
        assert draft_store.auto_selectable(draft), "one failure is still worth a retry"

        draft["publish_attempts"] = 2
        draft_store.save(draft, directory)
        assert draft_store.is_parked(draft)
        assert not draft_store.auto_selectable(draft)
        selected, _, _ = _with_draft_dir(directory, publish_drafts.select_drafts)
        assert selected == [], "a parked draft must not be picked up automatically"


def test_a_parked_draft_can_still_be_forced_by_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = _batch(directory, "20260728T114005", "1005006000000003",
                       status=draft_store.STATUS_PUBLISH_FAILED, publish_attempts=5)
        draft_store.save(draft, directory)
        selected, _, notes = _with_draft_dir(
            directory, publish_drafts.select_drafts, only_draft_id=draft["draft_id"]
        )
        assert len(selected) == 1
        assert any("parked" in note for note in notes)


def test_a_failed_publish_increments_the_attempt_count() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        _publish(draft, _StubListOne(fail="eBay refused"), directory)
        first = draft_store.load(draft["draft_id"], directory)
        assert draft_store.attempts(first) == 1
        assert not draft_store.is_parked(first)

        _publish(first, _StubListOne(fail="eBay refused"), directory)
        second = draft_store.load(draft["draft_id"], directory)
        assert draft_store.attempts(second) == 2
        assert draft_store.is_parked(second), "the second refusal parks it"
        assert "parked" in second["publish_error"]


def test_a_cost_block_does_not_count_as_an_attempt() -> None:
    """A stale price is our refusal, not eBay's — it should not burn a retry."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        original_ali, original_dir = publish_drafts.ali_api, draft_store.DRAFT_DIR
        publish_drafts.ali_api = _FakeAli("30.00")  # +40% cost
        draft_store.DRAFT_DIR = directory
        try:
            with tempfile.TemporaryDirectory() as runs:
                outcome = publish_drafts.publish_one(draft, object(), {}, Path(runs))
        finally:
            publish_drafts.ali_api = original_ali
            draft_store.DRAFT_DIR = original_dir
        assert outcome["status"] == "blocked"
        assert draft_store.attempts(draft_store.load(draft["draft_id"], directory)) == 0


# ---------------------------------------------------------- independence from the sheet

def test_publishing_never_reads_the_sheet() -> None:
    """Selection comes from disk, so a Sheets outage cannot stop a publish."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _batch(directory, "20260728T114005", "1005006000000003")

        def explode(*args, **kwargs):
            raise AssertionError("publishing must not read the Drafts tab")

        original = getattr(draft_sheet, "read_all_rows", None)
        draft_sheet.read_all_rows = explode
        try:
            selected, batch, _ = _with_draft_dir(directory, publish_drafts.select_drafts)
        finally:
            if original is not None:
                draft_sheet.read_all_rows = original
        assert len(selected) == 1 and batch == "20260728T114005"


def test_the_sheet_has_no_approval_column() -> None:
    """A control that no longer controls anything is worse than no control."""
    assert "Publish?" not in draft_sheet.HEADERS
    assert "Batch" in draft_sheet.HEADERS
    for gone in ("read_approved", "row_edits", "apply_edits", "is_approved"):
        assert not hasattr(draft_sheet, gone), f"{gone} should have been removed"


def test_the_row_shows_which_batch_it_belongs_to() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        draft = sample_draft(Path(tmp))
        row = draft_sheet.draft_row(draft)
        assert row[draft_sheet.COL["Batch"]] == draft["run_stamp"]


def test_publish_result_email_reports_pending_and_parked() -> None:
    subject, text, _ = notify.compose({
        "status": "listed", "date": "2026-07-28", "listed_count": 1, "expected_count": 1,
        "products": [],
        "pending": [{"run_stamp": "20260727T212942", "title": "Tesla Sun Shade"}],
        "parked": [{"title": "Dr Pen Ultima M8S", "attempts": 2, "error": "eBay policy"}],
    })
    assert "1 draft(s) still pending" in text
    assert "Tesla Sun Shade" in text
    assert "parked after repeated eBay refusals" in text
    assert "Dr Pen Ultima M8S" in text


def test_drafts_email_lists_older_pending_drafts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        draft = sample_draft(Path(tmp))
        _, text, _ = notify.compose_drafts({
            "date": "2026-07-28", "niche": "hobby", "drafts": [draft], "draft_count": 1,
            "pending_older": [
                {"created_at": "2026-07-27T21:29:42Z", "title": "Tesla Sun Shade", "parked": False},
                {"created_at": "2026-07-27T21:29:42Z", "title": "Dr Pen", "parked": True},
            ],
        })
        assert "2 older draft(s) are still waiting" in text
        assert "Tesla Sun Shade" in text
        assert "[parked" in text


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
