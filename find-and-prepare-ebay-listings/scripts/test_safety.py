#!/usr/bin/env python3
"""Safety tests: a run must never publish to eBay unless --live is passed explicitly.

A test run once published a real listing because publishing was the default. These tests
lock the safe default in place.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPTS = Path(__file__).parent
WORKFLOW = SCRIPTS.parent.parent / ".github" / "workflows" / "daily.yml"


def test_daily_run_defaults_to_dry_run() -> None:
    source = (SCRIPTS / "daily_run.py").read_text(encoding="utf-8")
    assert '"--live"' in source, "a --live flag must exist"
    assert "dry_run = not args.live" in source, "dry run must be the default"


def test_publishing_is_gated_on_live_flag() -> None:
    """run(dry_run=True) must return before importing/calling the eBay lister."""
    source = (SCRIPTS / "daily_run.py").read_text(encoding="utf-8")
    dry_index = source.index("if dry_run:")
    import_index = source.index("from ebay_listing import list_resilient")
    assert dry_index < import_index, "the dry-run early return must precede any eBay call"


def test_workflow_has_no_active_schedule() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    active_schedule = [
        line for line in text.splitlines()
        if re.match(r"^\s*-\s*cron:", line) and not line.lstrip().startswith("#")
    ]
    assert not active_schedule, f"automation must stay paused, found: {active_schedule}"


def test_workflow_defaults_to_a_safe_mode() -> None:
    """The manual run mode must default to dry-run, so 'Run workflow' can't list."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "mode:" in text, "workflow needs a 'mode' input"
    block = text.split("mode:", 1)[1][:400]
    assert "default: dry-run" in block, "mode must default to dry-run"


def test_workflow_publishes_only_in_full_mode() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--live" in text, "workflow must pass --live to publish"
    live_line = next(line for line in text.splitlines() if line.strip().startswith("LIVE:"))
    assert "inputs.mode == 'full'" in live_line, "only mode=full may publish"
    assert "LIVE_LISTING" in live_line, "the kill switch must gate the LIVE decision"


def test_workflow_has_kill_switch() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "LIVE_LISTING" in text, "a LIVE_LISTING kill switch must exist"


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
