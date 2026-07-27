#!/usr/bin/env python3
"""Safety tests for the publish path.

The daily_run.py entrypoint stays dry unless --live is passed explicitly. By design the
workflow now defaults its manual mode to "draft" (review before listing) and lets mode
alone decide — there is no separate LIVE_LISTING kill switch. Scheduled runs draft unless
the repository variable LISTING_MODE is set to "auto", which restores the original
publish-immediately behaviour. These tests lock that intended behavior in place.
"""

from __future__ import annotations

import ast
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


def test_workflow_schedule_is_exactly_9am_ist() -> None:
    # Automation is intentionally live (user-approved): daily at 09:00 IST = 03:30 UTC,
    # no DST in IST. A schedule change here must be a deliberate edit, not a drift.
    text = WORKFLOW.read_text(encoding="utf-8")
    active_schedule = [
        line for line in text.splitlines()
        if re.match(r"^\s*-\s*cron:", line) and not line.lstrip().startswith("#")
    ]
    assert len(active_schedule) == 1, f"expected exactly one active schedule, found: {active_schedule}"
    assert '"30 3 * * *"' in active_schedule[0], f"expected 09:00 IST (30 3 * * *), found: {active_schedule}"


def test_workflow_defaults_to_draft_mode() -> None:
    """'Run workflow' must default to drafting for review, not to publishing."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "mode:" in text, "workflow needs a 'mode' input"
    block = text.split("mode:", 1)[1][:500]
    assert "default: draft" in block, "mode must default to draft"
    assert "full" in block, "publishing immediately must remain available as an option"
    assert "dry-run" in block, "dry-run must remain available as an option"


def test_workflow_publishes_only_on_full_mode_or_explicit_auto() -> None:
    """A scheduled run publishes only when LISTING_MODE is explicitly set to 'auto'."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--live" in text, "workflow must pass --live to publish"
    live_line = next(line for line in text.splitlines() if line.strip().startswith("LIVE:"))
    assert "inputs.mode == 'full'" in live_line, "mode=full drives the LIVE decision"
    assert "vars.LISTING_MODE == 'auto'" in live_line, "scheduled publishing must be opt-in"


def test_scheduled_runs_draft_by_default() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--draft" in text, "workflow must be able to pass --draft"
    draft_line = next(line for line in text.splitlines() if line.strip().startswith("DRAFT:"))
    assert "github.event_name == 'schedule'" in draft_line, "scheduled runs must draft"
    assert "vars.LISTING_MODE != 'auto'" in draft_line, "drafting is the default for the schedule"


def test_draft_mode_creates_nothing_on_ebay() -> None:
    """Drafting validates read-only; the offer-creating path must stay behind --live."""
    source = (SCRIPTS / "daily_run.py").read_text(encoding="utf-8")
    build = source.index("def build_drafts(")
    run_start = source.index("def run(mode: str)")
    assert "validate_for_draft" in source[build:run_start], "drafting must use read-only validation"
    assert "prepare_product" not in source, "daily_run must never create eBay offers directly"


def test_workflow_has_no_kill_switch() -> None:
    """The LIVE_LISTING kill switch was removed on purpose; mode alone decides."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "LIVE_LISTING" not in text, "LIVE_LISTING kill switch must not return"


def _required_parameters(node: ast.FunctionDef) -> list[str]:
    """Parameters the caller must supply — those without a default. *args/**kwargs and
    parameters with defaults are fine, because the no-argument runner can still call."""
    arguments = node.args
    positional = list(arguments.posonlyargs) + list(arguments.args)
    without_default = positional[: len(positional) - len(arguments.defaults)]
    required = [item.arg for item in without_default]
    required += [
        item.arg for item, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
        if default is None
    ]
    return required


def _hands_off_to_unittest(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Attribute)
        and node.attr == "main"
        and isinstance(node.value, ast.Name)
        and node.value.id == "unittest"
        for node in ast.walk(tree)
    )


def test_every_test_function_is_actually_collected() -> None:
    """Guard against tests that silently never run.

    These files are plain scripts: `_run_all` collects `test_*` out of `globals()` at
    the bottom of the file. That gives three ways to write a test that never executes
    and so always "passes" — the first two have already happened here:

      * defining it below `_run_all`, where globals() has not seen it yet;
      * giving it a *required* pytest fixture parameter (`monkeypatch`), which the runner
        cannot supply, so the call raises TypeError instead of testing anything. A
        parameter with a default (`tmp_path: Path | None = None`) is fine — it runs;
      * writing module-level tests in a file with no runner at all, so `python3 file.py`
        exits 0 having executed nothing.

    A test that cannot fail is worse than no test, so fail the suite on any of them.

    Parsed with `ast` rather than searched as text: this file contains the literal
    "_run_all" in its own source, so a text search finds that instead of the real
    definition and misjudges every test defined after this one.
    """
    for path in sorted(SCRIPTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_tests = [
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]
        runner = next(
            (node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name == "_run_all"),
            None,
        )

        if runner is None:
            # No plain-script runner. That is only acceptable when the file hands off to
            # unittest, which discovers its own TestCase methods (test_skill.py). A
            # runnerless file holding module-level tests executes none of them.
            assert _hands_off_to_unittest(tree), (
                f"{path.name} has neither a _run_all runner nor unittest.main() — "
                "running it would execute nothing"
            )
            assert not module_tests, (
                f"{path.name} is unittest-based but defines module-level test "
                f"function(s) {[node.name for node in module_tests]}, which unittest "
                "does not discover — move them into a TestCase"
            )
            continue

        assert module_tests, f"{path.name} defines no module-level test functions"
        for node in module_tests:
            assert node.lineno < runner.lineno, (
                f"{path.name}: {node.name} (line {node.lineno}) is defined below "
                f"_run_all (line {runner.lineno}) and will never be collected"
            )
            required = _required_parameters(node)
            assert not required, (
                f"{path.name}: {node.name} requires {', '.join(required)!r}, but the "
                "plain-script runner passes no arguments — it would error, not test"
            )


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
