#!/usr/bin/env python3
"""Offline tests for the picked-products lister (one text box -> one or two listings).

Never hits the network or eBay: the AliExpress detail fetch is monkeypatched to the
fixture, redirect following is switched off, and --live is never exercised."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import ali_api
import list_picked

FIXTURE = Path(__file__).with_name("fixtures") / "ali_sample.json"

URL_1 = "https://www.aliexpress.us/item/1005006000000001.html"
URL_2 = "https://www.aliexpress.com/item/1005006000000002.html?spm=a2g0o.x"


def _details() -> list[dict]:
    os.environ["ALI_API_FIXTURE"] = str(FIXTURE)
    return ali_api.discover("anything", 1)


def _extract(text: str) -> tuple[list[str], list[str]]:
    return list_picked.extract_urls(text, follow_redirects=False)


# --- extracting links out of pasted text ------------------------------------------

def test_one_link_yields_one_product() -> None:
    urls, notes = _extract(URL_1)
    assert urls == ["https://www.aliexpress.us/item/1005006000000001.html"]
    assert not notes


def test_two_links_keep_paste_order() -> None:
    urls, _ = _extract(f"{URL_1}\n{URL_2}")
    assert urls == [
        "https://www.aliexpress.us/item/1005006000000001.html",
        "https://www.aliexpress.us/item/1005006000000002.html",
    ]


def test_separators_and_surrounding_prose() -> None:
    blobs = [
        f"{URL_1}, {URL_2}",
        f"{URL_1} {URL_2}",
        f"first one {URL_1} and this one too {URL_2} thanks",
        f"1. {URL_1}\n2. {URL_2}\n",
    ]
    for blob in blobs:
        urls, _ = _extract(blob)
        assert len(urls) == 2, blob


def test_trailing_punctuation_is_not_part_of_the_link() -> None:
    urls, _ = _extract(f"look at {URL_1}. Also worth a look.")
    assert urls == ["https://www.aliexpress.us/item/1005006000000001.html"]


def test_same_product_in_two_forms_collapses() -> None:
    other_form = "https://www.aliexpress.com/i/1005006000000001.html"
    urls, _ = _extract(f"{URL_1}\n{other_form}")
    assert urls == ["https://www.aliexpress.us/item/1005006000000001.html"]


def test_non_aliexpress_links_are_ignored() -> None:
    urls, notes = _extract(f"https://www.ebay.com/itm/123456789 {URL_1} https://example.com/x")
    assert urls == ["https://www.aliexpress.us/item/1005006000000001.html"]
    assert not notes, "unrelated links must be ignored silently, not reported"


def test_text_without_any_link_is_an_error() -> None:
    for blob in ("", "   ", "I want to sell some phone cases"):
        try:
            _extract(blob)
        except list_picked.PickError:
            continue
        raise AssertionError(f"expected PickError for {blob!r}")


def test_aliexpress_link_without_an_id_is_skipped_with_a_note() -> None:
    urls, notes = _extract(f"https://www.aliexpress.us/category/shoes {URL_1}")
    assert urls == ["https://www.aliexpress.us/item/1005006000000001.html"]
    assert any("Skipped" in note for note in notes)


def test_share_link_is_skipped_when_redirects_are_off() -> None:
    # follow_redirects=False keeps this test offline; the note tells you what to do.
    urls, notes = _extract(f"https://a.aliexpress.com/_mNxpEcv {URL_1}")
    assert urls == ["https://www.aliexpress.us/item/1005006000000001.html"]
    assert any("share link" in note or "no AliExpress product id" in note for note in notes)


def test_too_many_links_are_refused() -> None:
    blob = " ".join(
        f"https://www.aliexpress.us/item/10050060000000{n:02d}.html" for n in range(1, 12)
    )
    try:
        _extract(blob)
    except list_picked.PickError as exc:
        assert "at most" in str(exc)
        return
    raise AssertionError("expected PickError for a bulk paste")


def test_max_products_is_configurable() -> None:
    original = list_picked.MAX_PRODUCTS
    list_picked.MAX_PRODUCTS = 1
    try:
        _extract(f"{URL_1} {URL_2}")
    except list_picked.PickError:
        return
    finally:
        list_picked.MAX_PRODUCTS = original
    raise AssertionError("MAX_PRODUCTS must cap extraction")


# --- the dry-run pipeline ----------------------------------------------------------

def _with_fixture_details(text: str, indexes: list[int], tmp: str) -> dict:
    """Run list_picked over the fixture products at ``indexes``, serving each detail in
    the order the products are requested."""
    details = _details()
    queue = [details[i] for i in indexes]
    original = ali_api.get_product_detail
    original_runs = list_picked.RUNS_DIR
    ali_api.get_product_detail = lambda pid: queue.pop(0)
    list_picked.RUNS_DIR = Path(tmp)
    try:
        return list_picked.list_picked(text, live=False, follow_redirects=False)
    finally:
        ali_api.get_product_detail = original
        list_picked.RUNS_DIR = original_runs


def test_dry_run_over_two_links_prepares_two_sources() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = _with_fixture_details(f"{URL_1}\n{URL_2}", [0, 1], tmp)
        sources = sorted(Path(tmp).glob("picked-*/product-*/source.json"))
    assert result["status"] == "partial"        # dry run
    assert result["listed_count"] == 0
    assert result["expected_count"] == 2
    assert result["niche"] == "picked"
    assert len(sources) == 2, sources
    assert [p["product_id"] for p in result["products"]] == [
        "1005006000000001", "1005006000000002",
    ]
    assert all(p["ebay_url"] == "" for p in result["products"]), "nothing may be published"


def test_dry_run_over_one_link_expects_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = _with_fixture_details(URL_1, [0], tmp)
        sources = sorted(Path(tmp).glob("picked-*/product-*/source.json"))
    assert result["expected_count"] == 1
    assert len(sources) == 1
    assert len(result["products"]) == 1


def test_gate_failure_warns_but_still_prepares() -> None:
    """The whole point of this path: you picked it, so a gate miss is advisory."""
    with tempfile.TemporaryDirectory() as tmp:
        # fixture[3] is "reviews < MIN_REVIEWS"; fixture[5] is "price < MIN_PRICE_USD".
        result = _with_fixture_details(
            "https://www.aliexpress.us/item/1005006000000004.html "
            "https://www.aliexpress.us/item/1005006000000006.html",
            [3, 5], tmp,
        )
        sources = sorted(Path(tmp).glob("picked-*/product-*/source.json"))
    assert len(sources) == 2, "a gate miss must not block preparation"
    assert result["status"] == "partial"
    warnings = [n for n in result["notes"] if "Quality-gate warning" in n]
    assert len(warnings) == 2, result["notes"]
    assert any("reviews <" in n for n in warnings)
    assert any("price <" in n for n in warnings)


def test_one_bad_link_never_blocks_its_sibling() -> None:
    details = _details()
    calls: list[str] = []

    def fake_detail(pid: str) -> dict:
        calls.append(pid)
        if pid == "1005006000000002":
            raise ali_api.AliError("product not found")
        return details[0]

    original, original_runs = ali_api.get_product_detail, list_picked.RUNS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        ali_api.get_product_detail = fake_detail
        list_picked.RUNS_DIR = Path(tmp)
        try:
            result = list_picked.list_picked(
                f"{URL_1}\n{URL_2}", live=False, follow_redirects=False)
        finally:
            ali_api.get_product_detail = original
            list_picked.RUNS_DIR = original_runs
        sources = sorted(Path(tmp).glob("picked-*/product-*/source.json"))
    assert calls == ["1005006000000001", "1005006000000002"], "both links must be tried"
    assert len(sources) == 1, "the good product is still prepared"
    assert len(result["products"]) == 1
    assert any("Could not prepare" in n for n in result["notes"])


def test_all_links_bad_is_a_clean_error() -> None:
    original, original_runs = ali_api.get_product_detail, list_picked.RUNS_DIR

    def fail(pid: str) -> dict:
        raise ali_api.AliError("product not found")

    with tempfile.TemporaryDirectory() as tmp:
        ali_api.get_product_detail = fail
        list_picked.RUNS_DIR = Path(tmp)
        try:
            result = list_picked.list_picked(URL_1, live=False, follow_redirects=False)
        finally:
            ali_api.get_product_detail = original
            list_picked.RUNS_DIR = original_runs
    assert result["status"] == "error"
    assert "None of the pasted links" in result["error"]


def test_bad_text_returns_error_rather_than_raising() -> None:
    result = list_picked.list_picked("nothing here", live=False, follow_redirects=False)
    assert result["status"] == "error"
    assert "No AliExpress product links" in result["error"]
    assert result["expected_count"] == 0


# --- safety posture ----------------------------------------------------------------

def test_cli_defaults_to_dry_run() -> None:
    source = Path(__file__).with_name("list_picked.py").read_text(encoding="utf-8")
    assert '"--live"' in source, "a --live flag must exist"
    assert "live=args.live" in source, "the CLI must pass the flag through"


def test_publishing_is_gated_on_the_live_flag() -> None:
    """The eBay lister must not even be imported on a dry run."""
    source = Path(__file__).with_name("list_picked.py").read_text(encoding="utf-8")
    dry_index = source.index("if not live:")
    import_index = source.index("from ebay_listing import list_resilient")
    assert dry_index < import_index, "the dry-run early return must precede any eBay call"


def test_workflow_passes_links_through_the_environment() -> None:
    """`${{ inputs.links }}` inline in a run block would be a shell injection."""
    workflow = Path(__file__).parents[2] / ".github" / "workflows" / "list-picked.yml"
    text = workflow.read_text(encoding="utf-8")
    assert 'LINKS: ${{ inputs.links }}' in text, "links must be bound to an env var"
    assert '--links "$LINKS"' in text, "the script must read the env var"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("python ") or stripped.startswith("echo "):
            assert "inputs.links" not in stripped, f"link text interpolated into: {stripped}"


def test_workflow_has_no_schedule() -> None:
    workflow = Path(__file__).parents[2] / ".github" / "workflows" / "list-picked.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "schedule:" not in text, "this workflow is a button, never a cron"
    assert "workflow_dispatch:" in text


def _run_all() -> int:
    tests = [v for n, v in sorted(globals().items()) if n.startswith("test_") and callable(v)]
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
