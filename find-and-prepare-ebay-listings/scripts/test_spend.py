#!/usr/bin/env python3
"""Offline tests for the OpenAI spend ledger."""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path


def _fresh_spend(tmp: Path):
    os.environ["SPEND_LEDGER"] = str(tmp / "openai-spend.jsonl")
    import spend as spend_module

    return importlib.reload(spend_module)


def test_cost_math_matches_price_table() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spend = _fresh_spend(Path(tmp))
        # gpt-4.1-mini: $0.40/1M in, $1.60/1M out
        cost = spend.cost_usd("gpt-4.1-mini", 1_000_000, 1_000_000)
        assert abs(cost - 2.00) < 1e-9, cost
        assert abs(spend.cost_usd("gpt-4.1-mini", 1700, 800) - 0.00196) < 1e-6


def test_record_and_totals_roll_up() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spend = _fresh_spend(Path(tmp))
        spend.record("gpt-4.1-mini", 1000, 500, purpose="ranking")
        spend.record("gpt-4.1-mini", 1000, 500, purpose="listing_copy")
        totals = spend.totals()
        expected = 2 * spend.cost_usd("gpt-4.1-mini", 1000, 500)
        assert abs(totals["today"] - round(expected, 4)) < 1e-4
        assert abs(totals["all_time"] - round(expected, 4)) < 1e-4
        assert abs(totals["month_to_date"] - round(expected, 4)) < 1e-4


def test_totals_empty_when_no_ledger() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spend = _fresh_spend(Path(tmp))
        assert spend.totals() == {"today": 0.0, "month_to_date": 0.0, "all_time": 0.0}


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
