#!/usr/bin/env python3
"""Offline tests for the draft-first review flow.

Covers the draft store, the Drafts-tab encoding round-trips, the reviewer's edits being
applied (and never overwritten), the price override, the stale-cost guard, and the
central safety property: drafting must not mutate anything on eBay.

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


def test_publish_column_accepts_the_many_ways_to_tick_it() -> None:
    for value in ("YES", "yes", "Yes", "TRUE", "true", "1", "x", "✓"):
        assert draft_sheet.is_approved(value), value
    for value in ("", "NO", "no", "FALSE", "later", None):
        assert not draft_sheet.is_approved(value), value


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


def test_row_edits_are_applied_to_the_source() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        draft = sample_draft(Path(tmp))
        row = _row_for(
            draft,
            **{
                "Title": "Hand-written RC Drift Car Title",
                "Images": "https://ae01.alicdn.com/kf/a.jpg\nhttps://ae01.alicdn.com/kf/spare.jpg",
                "Item Specifics": "Brand: Unbranded\nMPN: N/A\nType: Drift Buggy",
                "Category ID": "182183",
            },
        )
        merged = draft_sheet.apply_edits(draft, draft_sheet.row_edits(row, draft))
        assert merged["listing_title"] == "Hand-written RC Drift Car Title"
        assert merged["source_images"][-1] == "https://ae01.alicdn.com/kf/spare.jpg"
        assert merged["aspects"]["Type"] == ["Drift Buggy"]
        assert merged["category_id_override"] == "182183"
        # normalize_source must accept the edited result unchanged.
        assert normalize_source(merged)["listing_title"] == "Hand-written RC Drift Car Title"


def test_clearing_a_cell_falls_back_to_the_stored_draft() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        draft = sample_draft(Path(tmp))
        row = _row_for(draft, **{"Title": "", "Item Specifics": "", "Images": ""})
        merged = draft_sheet.apply_edits(draft, draft_sheet.row_edits(row, draft))
        assert merged["listing_title"] == draft["source"]["listing_title"]
        assert merged["source_images"] == draft["source"]["source_images"]


def test_editing_a_draft_never_mutates_the_stored_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        draft = sample_draft(Path(tmp))
        original = json.dumps(draft["source"], sort_keys=True)
        row = _row_for(draft, **{"Title": "Something else entirely"})
        draft_sheet.apply_edits(draft, draft_sheet.row_edits(row, draft))
        assert json.dumps(draft["source"], sort_keys=True) == original


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


def test_sheet_price_override_reaches_every_variant() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        draft = sample_draft(Path(tmp))
        row = _row_for(draft, **{"Price Override USD": "88.00"})
        merged = draft_sheet.apply_edits(draft, draft_sheet.row_edits(row, draft))
        assert all(v["price_override"] == "88.00" for v in merged["selected_variants"])
        assert normalize_source(merged)["selected_variants"][0]["expected_ebay_price"] == "88.00"


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
    source = normalize_source(sample_source())
    source["selected_variants"][0]["price_override"] = "88.00"
    ok, _, refreshed = _with_fake_ali(_FakeAli("14.99"), source)
    assert ok
    assert refreshed["selected_variants"][0]["delivered_total"] == "24.50"


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


def test_publish_workflow_is_opt_in() -> None:
    workflow = (SCRIPTS.parent.parent / ".github" / "workflows" / "publish-drafts.yml").read_text(
        encoding="utf-8"
    )
    assert "default: dry-run" in workflow, "publishing must be opt-in"
    assert "--live" in workflow, "publish mode must pass --live"


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


def _publish(draft, edits, stub, directory):
    import ebay_listing

    original_list_one = ebay_listing.list_one
    original_dir = draft_store.DRAFT_DIR
    original_ali = publish_drafts.ali_api
    ebay_listing.list_one = stub
    draft_store.DRAFT_DIR = directory
    publish_drafts.ali_api = _FakeAli("19.99")
    try:
        with tempfile.TemporaryDirectory() as runs:
            return publish_drafts.publish_one(draft, edits, object(), {}, Path(runs))
    finally:
        ebay_listing.list_one = original_list_one
        draft_store.DRAFT_DIR = original_dir
        publish_drafts.ali_api = original_ali


def test_publish_sends_the_reviewed_copy_with_enrichment_off() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        row = _row_for(draft, **{
            "Title": "Reviewer's Own Title",
            "Price Override USD": "99.00",
            "Images": "https://ae01.alicdn.com/kf/a.jpg\nhttps://ae01.alicdn.com/kf/spare.jpg",
        })
        stub = _StubListOne()
        outcome = _publish(draft, draft_sheet.row_edits(row, draft), stub, directory)

        assert outcome["status"] == "live"
        assert outcome["listing_id"] == "1122334455"
        # The reviewer's edits, not the drafted copy, are what reached eBay.
        assert stub.seen["source"]["listing_title"] == "Reviewer's Own Title"
        assert stub.seen["source"]["selected_variants"][0]["expected_ebay_price"] == "99.00"
        assert "https://ae01.alicdn.com/kf/spare.jpg" in stub.seen["source"]["source_images"]
        assert stub.seen["enrich"] is False, "publishing must not re-run the AI over the edits"

        stored = draft_store.load(draft["draft_id"], directory)
        assert stored["status"] == "live"
        assert stored["published"] is True
        assert stored["ebay_url"] == "https://www.ebay.com/itm/1122334455"


def test_a_failed_publish_leaves_the_draft_retryable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        stub = _StubListOne(fail="eBay rejected the category")
        outcome = _publish(draft, {}, stub, directory)

        assert outcome["status"] == "publish_failed"
        stored = draft_store.load(draft["draft_id"], directory)
        assert stored["published"] is False
        assert stored["publish_error"] == "eBay rejected the category"
        assert draft_store.is_publishable(stored), "a failed draft must be fixable and re-approved"


def test_an_invalid_edit_is_caught_before_ebay_is_touched() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = sample_draft(directory)
        # An override at cost is rejected by normalize_source.
        row = _row_for(draft, **{"Price Override USD": "24.50"})
        stub = _StubListOne()
        outcome = _publish(draft, draft_sheet.row_edits(row, draft), stub, directory)

        assert outcome["status"] == "blocked"
        assert stub.seen == {}, "eBay must not be called with an invalid source"
        assert draft_store.is_publishable(draft_store.load(draft["draft_id"], directory))


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
                outcome = publish_drafts.publish_one(draft, {}, object(), {}, Path(runs))
        finally:
            ebay_listing.list_one = original_list_one
            draft_store.DRAFT_DIR = original_dir
            publish_drafts.ali_api = original_ali

        assert outcome["status"] == "blocked"
        assert stub.seen == {}, "a stale draft must never reach eBay"
        assert draft_store.is_publishable(draft_store.load(draft["draft_id"], directory))


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
        assert "Set Publish? to YES" in text
        assert "https://sheets.example/x" in text
        assert "<html>" in html


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
