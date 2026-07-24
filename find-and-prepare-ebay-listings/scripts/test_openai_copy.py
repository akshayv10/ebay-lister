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


def test_missing_key_raises_copyerror() -> None:
    os.environ.pop("OPENAI_API_KEY", None)
    try:
        openai_copy.generate_listing("x", "y", "1", [])
    except openai_copy.CopyError:
        return
    raise AssertionError("expected CopyError when OPENAI_API_KEY is unset")


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
