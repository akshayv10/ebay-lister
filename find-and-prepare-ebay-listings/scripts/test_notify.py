#!/usr/bin/env python3
"""Offline tests for notification rendering."""

from __future__ import annotations

import notify


def test_sheet_sync_success_is_in_both_bodies() -> None:
    _, text, html = notify.compose({
        "date": "2026-07-24",
        "status": "listed",
        "niche": "Test",
        "listed_count": 2,
        "products": [],
        "sheet_sync": {"status": "synced", "written": 2, "queued": 0, "error": ""},
    })
    assert "Google Sheets: synced (written 2, queued 0)" in text
    assert "Google Sheets: synced (written 2, queued 0)" in html
    assert "Open Auto Lister sheet" in html


def test_sheet_sync_queue_error_is_visible() -> None:
    _, text, html = notify.compose({
        "date": "2026-07-24",
        "status": "listed",
        "niche": "Test",
        "listed_count": 1,
        "products": [],
        "sheet_sync": {
            "status": "queued",
            "written": 0,
            "queued": 1,
            "error": "temporary outage",
        },
    })
    assert "Sync error: temporary outage" in text
    assert "Sync error: temporary outage" in html


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
