#!/usr/bin/env python3
"""Safety tests for the publish path.

The daily_run.py entrypoint stays dry unless --live is passed explicitly, and the
automation schedule stays paused. By design, the workflow now defaults its manual mode to
"full" (publishing) and lets mode alone decide — there is no separate LIVE_LISTING kill
switch. These tests lock that intended behavior in place.
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


def test_workflow_defaults_to_full_mode() -> None:
    """The manual run mode defaults to 'full' so 'Run workflow' publishes by default."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "mode:" in text, "workflow needs a 'mode' input"
    block = text.split("mode:", 1)[1][:400]
    assert "default: full" in block, "mode must default to full"
    assert "dry-run" in block, "dry-run must remain available as an option"


def test_workflow_publishes_only_in_full_or_scheduled_mode() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--live" in text, "workflow must pass --live to publish"
    live_line = next(line for line in text.splitlines() if line.strip().startswith("LIVE:"))
    assert "inputs.mode == 'full'" in live_line, "mode=full drives the LIVE decision"
    assert "github.event_name == 'schedule'" in live_line, "scheduled runs publish too"


def test_workflow_has_no_kill_switch() -> None:
    """The LIVE_LISTING kill switch was removed on purpose; mode alone decides."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "LIVE_LISTING" not in text, "LIVE_LISTING kill switch must not return"


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
