#!/usr/bin/env python3
"""Offline tests for openai_copy — mocked transport, never hits the network."""

from __future__ import annotations

import os

import openai_copy


def _fake_transport(body):
    # Mimic the OpenAI Responses API envelope with an output_text JSON string.
    assert body["model"]  # model is set
    assert any(part.get("type") == "input_image" for part in body["input"][1]["content"])  # images attached
    listing = {
        "title": "USB C Hub 6-in-1 Multiport Adapter 4K HDMI 100W PD 5Gbps Data for Laptop MacBook",
        "description": "<h3>Fast 6-in-1 hub</h3><ul><li>4K HDMI</li></ul>",
        "itemSpecifics": {"Type": "USB Hub", "Color": "Grey", "Country/Region of Manufacture": "China", "MPN": "X"},
    }
    return {"output_text": __import__("json").dumps(listing)}


def test_generate_listing_parses_and_returns_specifics() -> None:
    os.environ["OPENAI_API_KEY"] = "test-key"
    result = openai_copy.generate_listing(
        "USB C Hub Adapter", "USB Hubs", "18.00",
        ["https://x/main.jpg", "https://x/a.jpg"], transport=_fake_transport,
    )
    assert result["title"].startswith("USB C Hub")
    assert "<h3>" in result["description"]
    assert result["item_specifics"]["Type"] == "USB Hub"


def test_markdown_description_becomes_ebay_html() -> None:
    # eBay renders HTML; Markdown would show literal ** and --- to buyers.
    markdown = (
        "Intro sentence about the model. --- **Key Features:** "
        "- *Precision:* Accurate shape. - *Robust:* ABS plastic. - *Large:* 67cm / 26.4in."
    )
    html = openai_copy.to_html(markdown)
    assert "**" not in html and "---" not in html
    assert "<hr>" in html and "<strong>Key Features:</strong>" in html
    assert html.count("<li>") == 3


def test_existing_html_is_preserved() -> None:
    html = openai_copy.to_html("<p>Intro</p><h3>Features</h3><ul><li>One</li></ul>")
    assert html == "<p>Intro</p><h3>Features</h3><ul><li>One</li></ul>"


def test_generated_description_is_html_not_markdown() -> None:
    os.environ["OPENAI_API_KEY"] = "test-key"

    def markdown_transport(body):
        listing = {
            "title": "Formula Race Car Model Kit 1:8 Scale 1642 Pieces 67cm / 26.4in Display Build",
            "description": "Great build. --- **Specs:** - *Scale:* 1:8 - *Length:* 67cm - *Weight:* 1.2kg",
            "itemSpecifics": {"Type": "Model Kit"},
        }
        return {"output_text": __import__("json").dumps(listing)}

    result = openai_copy.generate_listing("Race car kit", "Models", "40.00", [], transport=markdown_transport)
    assert "**" not in result["description"]
    assert "<ul>" in result["description"]


def _fake_rank_transport(body):
    ranked = {"ranked": [
        {"id": "222", "score": 9, "reason": "RC drone, high gift appeal"},
        {"id": "111", "score": 6, "reason": "practical organiser"},
    ]}
    return {"output_text": __import__("json").dumps(ranked), "usage": {"input_tokens": 900, "output_tokens": 120}}


def test_rank_candidates_orders_by_score_and_reports_usage() -> None:
    os.environ["OPENAI_API_KEY"] = "test-key"
    result = openai_copy.rank_candidates(
        [{"id": "111", "title": "Desk organiser"}, {"id": "222", "title": "RC drone 4K"}],
        top_n=2, transport=_fake_rank_transport,
    )
    assert [r["id"] for r in result["ranked"]] == ["222", "111"]
    assert result["usage"] == (900, 120)


def test_missing_key_raises_copyerror() -> None:
    os.environ.pop("OPENAI_API_KEY", None)
    try:
        openai_copy.generate_listing("x", "y", "1", [])
    except openai_copy.CopyError:
        return
    raise AssertionError("expected CopyError when OPENAI_API_KEY is unset")


def test_brand_rules_and_brand_field_are_in_the_request() -> None:
    os.environ["OPENAI_API_KEY"] = "test-key"
    seen = {}

    def transport(body):
        seen["prompt"] = body["input"][1]["content"][0]["text"]
        seen["schema"] = body["text"]["format"]["schema"]
        return _fake_transport(body)

    openai_copy.generate_listing("USB C Hub", "USB Hubs", "18.00", ["https://x/a.jpg"], transport=transport)
    assert "BRAND NAMES" in seen["prompt"], "the no-brand instruction must reach the model"
    assert "brand" in seen["schema"]["properties"], "the model must report the brand it saw"


def test_reported_brand_is_stripped_from_the_generated_title() -> None:
    """Every listing goes out Brand=Unbranded, so a brand in the title contradicts it."""
    os.environ["OPENAI_API_KEY"] = "test-key"

    def branded_transport(body):
        listing = {
            "title": "Dr Pen Ultima M8S Microneedling Derma Pen Wireless Skin Care Tool 16 Pin 6 Speeds",
            "description": "<p>Adjustable microneedling pen.</p>",
            "itemSpecifics": {"Type": "Derma Pen"},
            "brand": "Dr Pen",
        }
        return {"output_text": __import__("json").dumps(listing)}

    result = openai_copy.generate_listing("Dr Pen M8S", "Beauty", "30.00", [], transport=branded_transport)
    assert "dr pen" not in result["title"].casefold(), result["title"]
    # What is left must still be a usable, tidy listing title.
    assert result["title"].startswith("Ultima M8S Microneedling"), result["title"]
    assert "  " not in result["title"] and result["detected_brand"] == "Dr Pen"


def test_all_brand_title_falls_back_to_the_template() -> None:
    os.environ["OPENAI_API_KEY"] = "test-key"

    def transport(body):
        listing = {"title": "Dr Pen Ultima", "description": "<p>x</p>",
                   "itemSpecifics": {}, "brand": "Dr Pen Ultima"}
        return {"output_text": __import__("json").dumps(listing)}

    try:
        openai_copy.generate_listing("x", "y", "1", [], transport=transport)
    except openai_copy.CopyError:
        return
    raise AssertionError("a title that is entirely brand must raise, not list a stub")


def test_strip_brands_edge_cases() -> None:
    strip = openai_copy.strip_brands
    # Global brands from the sourcing blocklist are caught with no help from the model.
    assert strip("Wireless Charger Stand for Samsung Galaxy S24") == "Wireless Charger Stand for S24"
    # Whole words only — a brand inside another word must survive.
    assert strip("Applesauce Maker Kitchen Tool") == "Applesauce Maker Kitchen Tool"
    # Longest match first, and separators left behind are tidied.
    assert strip("Under Armour - Gym Bag 40L") == "Gym Bag 40L"
    assert strip("Case + Cover for Sony", ["Sony"]) == "Case + Cover for"
    # Non-brand placeholders must never be stripped out of a title.
    assert strip("Unbranded Universal Phone Mount", ["Unbranded"]) == "Unbranded Universal Phone Mount"
    # Nothing to do.
    assert strip("6-in-1 USB C Hub 4K HDMI 100W PD") == "6-in-1 USB C Hub 4K HDMI 100W PD"


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
