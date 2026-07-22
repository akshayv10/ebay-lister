#!/usr/bin/env python3
"""Persist and enforce browser-work budgets for one eBay listing run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


LIMITS = {"browser_calls": 60, "dom_snapshots": 12, "browser_timeouts": 3}


class BudgetError(ValueError):
    pass


def write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def initialize(path: Path) -> dict[str, Any]:
    payload = {
        "status": "active",
        "limits": dict(LIMITS),
        "used": {key: 0 for key in LIMITS},
        "stages": {},
        "blocked_reason": None,
    }
    write(path, payload)
    return payload


def consume(path: Path, stage: str, **amounts: int) -> dict[str, Any]:
    if not stage.strip():
        raise BudgetError("stage is required")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") == "blocked":
        raise BudgetError(f"Run budget is blocked: {payload.get('blocked_reason')}")
    stage_used = payload.setdefault("stages", {}).setdefault(stage, {key: 0 for key in LIMITS})
    for key in LIMITS:
        amount = int(amounts.get(key, 0))
        if amount < 0:
            raise BudgetError(f"{key} increment cannot be negative")
        payload["used"][key] = int(payload["used"].get(key, 0)) + amount
        stage_used[key] = int(stage_used.get(key, 0)) + amount
    exceeded = [key for key, limit in LIMITS.items() if payload["used"][key] > limit]
    if exceeded:
        payload["status"] = "blocked"
        payload["blocked_reason"] = "budget_exceeded:" + ",".join(exceeded)
    write(path, payload)
    if exceeded:
        raise BudgetError(payload["blocked_reason"])
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--state", required=True, type=Path)
    add = sub.add_parser("consume")
    add.add_argument("--state", required=True, type=Path)
    add.add_argument("--stage", required=True)
    add.add_argument("--browser-calls", type=int, default=0)
    add.add_argument("--dom-snapshots", type=int, default=0)
    add.add_argument("--browser-timeouts", type=int, default=0)
    status = sub.add_parser("status")
    status.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "init":
            payload = initialize(args.state)
        elif args.command == "consume":
            payload = consume(
                args.state,
                args.stage,
                browser_calls=args.browser_calls,
                dom_snapshots=args.dom_snapshots,
                browser_timeouts=args.browser_timeouts,
            )
        else:
            payload = json.loads(args.state.read_text(encoding="utf-8"))
        print(json.dumps(payload))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
