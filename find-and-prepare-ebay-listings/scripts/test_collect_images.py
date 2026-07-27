#!/usr/bin/env python3
"""Offline tests for the local image collector.

No network: every fetch is stubbed with a saved-shape product page. Covers the
classification, the ordering rules that eBay and the AI copy depend on, the gallery
caps, and — most importantly — that a failed fetch never clears a gallery.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
from pathlib import Path
from typing import Any

import ali_api
import collect_images
import draft_sheet

# A product page shaped like AliExpress's: gallery and SKU images inside the inline
# runParams JSON (protocol-relative, with size suffixes), a description document link,
# and a thumbnail that must not reach the listing.
PAGE = """
<html><body>
<script>window.runParams = {"data": {
  "imageModule": {"imagePathList": [
      "//ae01.alicdn.com/kf/Smain1.jpg",
      "//ae01.alicdn.com/kf/Smain2.jpg_640x640.jpg_.webp",
      "//ae01.alicdn.com/kf/Smain1.jpg"
  ]},
  "skuModule": {"skuPriceList": [
      {"skuImage": "https://ae01.alicdn.com/kf/Sred.jpg"},
      {"skuImage": "https://ae01.alicdn.com/kf/Sblue.jpg"}
  ]},
  "descriptionModule": {"descriptionUrl": "https://aeproductsourcesite.alicdn.com/desc.htm"}
}};</script>
<img src="https://ae01.alicdn.com/kf/Sicon.jpg_50x50.jpg">
<img src="https://ae01.alicdn.com/kf/spacer.gif">
</body></html>
"""

DESCRIPTION_DOC = """
<div>
  <img src="//ae01.alicdn.com/kf/Sdesc1.jpg">
  <img src="//ae01.alicdn.com/kf/Sdesc2.png">
  <img src="//ae01.alicdn.com/kf/Smain1.jpg">
</div>
"""


class _Fetcher:
    """Stands in for ali_api.fetch_page."""

    def __init__(self, page: str = PAGE, description: str = DESCRIPTION_DOC):
        self.page = page
        self.description = description
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: int = 20) -> str:
        self.calls.append(url)
        return self.description if "desc.htm" in url else self.page


def _with_fetcher(fetcher, function, *args, **kwargs):
    original = ali_api.fetch_page
    ali_api.fetch_page = fetcher
    try:
        return function(*args, **kwargs)
    finally:
        ali_api.fetch_page = original


# ------------------------------------------------------------------- classification

def test_images_are_classified_by_source() -> None:
    sets = _with_fetcher(_Fetcher(), collect_images.collect, "https://x/item/1.html")
    assert sets["gallery"] == [
        "https://ae01.alicdn.com/kf/Smain1.jpg",
        "https://ae01.alicdn.com/kf/Smain2.jpg",
    ]
    assert sets["variant"] == [
        "https://ae01.alicdn.com/kf/Sred.jpg",
        "https://ae01.alicdn.com/kf/Sblue.jpg",
    ]
    # The description document contributes its own images, minus the one already seen.
    assert sets["description"] == [
        "https://ae01.alicdn.com/kf/Sdesc1.jpg",
        "https://ae01.alicdn.com/kf/Sdesc2.png",
    ]


def test_size_suffix_is_stripped_so_duplicates_collapse() -> None:
    """…_640x640.jpg_.webp and the bare URL are the same photo at different sizes."""
    assert ali_api._canonical_image("//ae01.alicdn.com/kf/S2.jpg_640x640.jpg_.webp") == \
        "https://ae01.alicdn.com/kf/S2.jpg"
    sets = _with_fetcher(_Fetcher(), collect_images.collect, "https://x/item/1.html")
    assert len(sets["gallery"]) == len(set(sets["gallery"]))


def test_thumbnails_and_animations_are_rejected() -> None:
    assert not ali_api.is_listable_image("https://ae01.alicdn.com/kf/Sicon.jpg_50x50.jpg")
    assert not ali_api.is_listable_image("https://ae01.alicdn.com/kf/spacer.gif")
    assert ali_api.is_listable_image("https://ae01.alicdn.com/kf/Smain1.jpg")
    everything = _with_fetcher(_Fetcher(), collect_images.collect, "https://x/item/1.html")
    flat = sum(everything.values(), [])
    assert not any("50x50" in url or url.endswith(".gif") for url in flat)


def test_description_images_can_be_skipped() -> None:
    sets = _with_fetcher(_Fetcher(), collect_images.collect, "https://x/item/1.html",
                         include_description=False)
    assert sets["description"] == []
    assert sets["gallery"], "the gallery must still be collected"


def test_a_blocked_page_yields_nothing_rather_than_raising() -> None:
    sets = _with_fetcher(lambda url, timeout=20: "", collect_images.collect, "https://x/item/1.html")
    assert sets == {"gallery": [], "variant": [], "description": []}


# ----------------------------------------------------------------------- ordering

def test_existing_primary_image_keeps_its_place() -> None:
    """eBay uses image 1 as the listing thumbnail, so it must not be displaced."""
    sets = _with_fetcher(_Fetcher(), collect_images.collect, "https://x/item/1.html")
    gallery, _ = collect_images.merge(["https://ae01.alicdn.com/kf/Schosen.jpg"], sets, 24)
    assert gallery[0] == "https://ae01.alicdn.com/kf/Schosen.jpg"


def test_description_images_never_lead() -> None:
    """openai_copy only reads the first 8 images — a size chart must not be one of them."""
    sets = _with_fetcher(_Fetcher(), collect_images.collect, "https://x/item/1.html")
    gallery, _ = collect_images.merge([], sets, 24)
    first_description = min(gallery.index(url) for url in sets["description"])
    last_clean = max(gallery.index(url) for url in sets["gallery"] + sets["variant"])
    assert first_description > last_clean


def test_merge_deduplicates_against_what_is_already_there() -> None:
    sets = _with_fetcher(_Fetcher(), collect_images.collect, "https://x/item/1.html")
    gallery, _ = collect_images.merge(["https://ae01.alicdn.com/kf/Smain1.jpg"], sets, 24)
    assert gallery.count("https://ae01.alicdn.com/kf/Smain1.jpg") == 1


# --------------------------------------------------------------------------- caps

def test_cap_is_12_for_multi_variant_and_24_otherwise() -> None:
    assert collect_images.image_cap(1) == 24
    assert collect_images.image_cap(3) == 12


def test_overflow_goes_to_spare_rather_than_being_dropped() -> None:
    sets = {"gallery": [f"https://ae01.alicdn.com/kf/S{i}.jpg" for i in range(20)],
            "variant": [], "description": []}
    gallery, overflow = collect_images.merge([], sets, 12)
    assert len(gallery) == 12
    assert len(overflow) == 8
    assert not set(gallery) & set(overflow)


def test_variant_count_drives_the_cap() -> None:
    values = [""] * len(draft_sheet.HEADERS)
    values[draft_sheet.COL["Variants"]] = "red | Color=Red | 1 | 2\nblue | Color=Blue | 1 | 2"
    assert collect_images.cap_for(values) == 12
    values[draft_sheet.COL["Variants"]] = "default |  | 19.99 | 24.50"
    assert collect_images.cap_for(values) == 24


def test_a_cleared_variants_cell_keeps_the_strict_cap() -> None:
    """Clearing an editable cell falls back to the stored draft at publish time, so the
    original variations return and >12 group images would then be rejected. Guessing
    "one variant" here would write 24 images that fail at publish."""
    values = [""] * len(draft_sheet.HEADERS)
    values[draft_sheet.COL["Variants"]] = ""
    assert collect_images.cap_for(values, "no-such-draft") == 12


def test_a_cleared_variants_cell_consults_the_stored_draft() -> None:
    import draft_store

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        draft = {
            "draft_id": "d-single",
            "source": {"selected_variants": [{"id": "default"}]},
        }
        (directory / "d-single.json").write_text(json.dumps(draft), encoding="utf-8")
        original = draft_store.DRAFT_DIR
        draft_store.DRAFT_DIR = directory
        try:
            values = [""] * len(draft_sheet.HEADERS)
            # Stored draft is single-variant, so the generous cap is safe after all.
            assert collect_images.cap_for(values, "d-single") == 24
        finally:
            draft_store.DRAFT_DIR = original


# ------------------------------------------- regressions from the PR #14 review

def test_change_detection_is_by_content_not_length() -> None:
    """Two size variants of one photo collapse to one, so a new image can arrive
    without the count moving — length alone would call that 'nothing to do'."""
    current = [
        "https://ae01.alicdn.com/kf/Sdup.jpg",
        "https://ae01.alicdn.com/kf/Sdup.jpg_640x640.jpg_.webp",
    ]
    sets = {"gallery": ["https://ae01.alicdn.com/kf/Snew.jpg"], "variant": [], "description": []}
    gallery, _ = collect_images.merge(current, sets, 24)
    assert len(gallery) == len(current), "the length is unchanged — this is the trap"
    assert collect_images.added_images(current, gallery) == ["https://ae01.alicdn.com/kf/Snew.jpg"]


def test_a_deduplicating_merge_is_still_written() -> None:
    """The exact skip-guard trap: two size variants of one photo collapse to one, and the
    page yields exactly one new image, so the gallery length never moves. Under the old
    length comparison this wrote nothing and the new photo was lost."""
    one_new_image = (
        '<script>window.runParams = {"data": {"imageModule": {"imagePathList":'
        ' ["//ae01.alicdn.com/kf/Sbrandnew.jpg"]}}};</script>'
    )
    values = _row("d1")
    values[draft_sheet.COL["Images"]] = (
        "https://ae01.alicdn.com/kf/Sdup.jpg\n"
        "https://ae01.alicdn.com/kf/Sdup.jpg_640x640.jpg_.webp"
    )
    rows = [{"draft_id": "d1", "row": 2, "values": values}]
    code, sheet = _run_collector(rows, lambda url, timeout=20: one_new_image)
    assert code == 0
    assert sheet.writes, "a merge that dedupes and adds must still be saved"
    written = next(
        item["values"][0][0] for item in sheet.writes[0][2]["data"]
        if item["range"].endswith(f"{chr(ord('A') + draft_sheet.COL['Images'])}2")
    )
    assert "Sbrandnew" in written, "the new image must reach the sheet"
    assert written.count("Sdup.jpg") == 1, "the duplicate must be collapsed"


def test_existing_warnings_are_kept() -> None:
    """draft_row writes real validation warnings there; overwriting hides what the
    reviewer needs before approving."""
    assert collect_images.merged_warning("Missing required eBay item specifics: Type", "3 from description") == \
        "Missing required eBay item specifics: Type | 3 from description"
    # Re-running must not stack duplicates.
    assert collect_images.merged_warning("a | b", "b") == "a | b"
    assert collect_images.merged_warning("", "only") == "only"
    assert collect_images.merged_warning("kept", "") == "kept"


def test_collector_appends_rather_than_replacing_the_warnings_cell() -> None:
    values = _row("d1")
    values[draft_sheet.COL["Warnings"]] = "Category/item-specific validation failed"
    rows = [{"draft_id": "d1", "row": 2, "values": values}]
    code, sheet = _run_collector(rows, _Fetcher())
    assert code == 0
    payload = sheet.writes[0][2]["data"]
    written = next(
        item["values"][0][0] for item in payload
        if item["range"].endswith(f"{chr(ord('A') + draft_sheet.COL['Warnings'])}2")
    )
    assert "Category/item-specific validation failed" in written, "the existing warning was lost"
    assert "description section" in written


# ------------------------------------------------------------------ sheet safety

class _FakeSheet:
    def __init__(self):
        self.sheet_name = "Drafts"
        self.headers = list(draft_sheet.HEADERS)
        self.writes: list[tuple] = []

    def ensure_sheet(self):
        return 1

    def request(self, method, path, payload=None, query=None):
        self.writes.append((method, path, payload))
        return {}


def _row(draft_id: str, **cells: Any) -> list[Any]:
    values = [""] * len(draft_sheet.HEADERS)
    values[0] = draft_id
    values[draft_sheet.COL["Status"]] = "draft"
    values[draft_sheet.COL["AliExpress URL"]] = "https://www.aliexpress.us/item/1005006000000123.html"
    for name, value in cells.items():
        values[draft_sheet.COL[name.replace("_", " ")]] = value
    return values


def _run_collector(rows, fetcher, **flags):
    sheet = _FakeSheet()
    original_client = draft_sheet.client
    original_rows = draft_sheet.read_all_rows
    original_disabled = draft_sheet.disabled
    draft_sheet.client = lambda **kw: sheet
    draft_sheet.read_all_rows = lambda **kw: rows
    draft_sheet.disabled = lambda: False
    options = {"url": None, "draft_id": "", "print_only": False, "browser": False,
               "no_description_images": False}
    options.update(flags)
    args = argparse.Namespace(**options)
    try:
        # The collector is chatty by design; keep the test output readable.
        with contextlib.redirect_stdout(io.StringIO()):
            code = _with_fetcher(fetcher, collect_images.run, args)
    finally:
        draft_sheet.client = original_client
        draft_sheet.read_all_rows = original_rows
        draft_sheet.disabled = original_disabled
    return code, sheet


def test_a_failed_fetch_leaves_the_row_untouched() -> None:
    """The gallery we already have is better than one we failed to replace."""
    rows = [{"draft_id": "d1", "row": 2, "values": _row("d1", Images="https://ae01.alicdn.com/kf/Skeep.jpg")}]
    code, sheet = _run_collector(rows, lambda url, timeout=20: "")
    assert code == 0
    assert sheet.writes == [], "a blocked page must not clear or rewrite the Images cell"


def test_print_only_writes_nothing() -> None:
    rows = [{"draft_id": "d1", "row": 2, "values": _row("d1")}]
    code, sheet = _run_collector(rows, _Fetcher(), print_only=True)
    assert code == 0
    assert sheet.writes == []


def test_collector_only_touches_its_own_columns() -> None:
    """It must never rewrite the whole row, or a title edit made meanwhile is reverted."""
    rows = [{"draft_id": "d1", "row": 2, "values": _row("d1", Title="Reviewer's title")}]
    code, sheet = _run_collector(rows, _Fetcher())
    assert code == 0
    assert len(sheet.writes) == 1
    ranges = {item["range"].split("!")[1] for item in sheet.writes[0][2]["data"]}
    allowed = {
        f"{chr(ord('A') + draft_sheet.COL[name])}2"
        for name in ("Images", "Spare Images", "Warnings")
    }
    assert ranges <= allowed, f"wrote outside its columns: {ranges - allowed}"


def test_live_and_rejected_drafts_are_skipped() -> None:
    rows = []
    for index, status in enumerate(("live", "rejected", "draft"), start=2):
        values = _row(f"d{index}")
        values[draft_sheet.COL["Status"]] = status
        rows.append({"draft_id": f"d{index}", "row": index, "values": values})
    code, sheet = _run_collector(rows, _Fetcher())
    assert code == 0
    assert len(sheet.writes) == 1, "only the pending draft should be written"


def test_description_count_is_surfaced_as_a_warning() -> None:
    rows = [{"draft_id": "d1", "row": 2, "values": _row("d1")}]
    code, sheet = _run_collector(rows, _Fetcher())
    assert code == 0
    payload = sheet.writes[0][2]["data"]
    warning = next(
        item["values"][0][0] for item in payload
        if item["range"].endswith(f"{chr(ord('A') + draft_sheet.COL['Warnings'])}2")
    )
    assert "description section" in warning


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
