#!/usr/bin/env python3
"""Select no more than four real combinations and exactly two when over the cap."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def number(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def eligible(combination: dict[str, Any]) -> bool:
    return bool(
        combination.get("available", True)
        and combination.get("meaningful", True)
        and combination.get("category_supported", True)
        and not combination.get("prohibited_global_brand", False)
    )


def rank_key(indexed: tuple[int, dict[str, Any]]) -> tuple[Any, ...]:
    index, item = indexed
    return (
        -number(item.get("aliexpress_popularity"), -1),
        number(item.get("delivery_days"), float("inf")),
        number(item.get("visible_item_price"), float("inf")),
        index,
        str(item.get("id", "")),
    )


def select_variants(payload: dict[str, Any]) -> dict[str, Any]:
    combinations = payload.get("combinations")
    if not isinstance(combinations, list):
        raise ValueError("combinations must be an array")
    filtered = [(index, item) for index, item in enumerate(combinations) if isinstance(item, dict) and eligible(item)]
    if not filtered:
        raise ValueError("No eligible variant combinations")
    if len(filtered) <= 4:
        selected = [item for _, item in filtered]
        policy = "all_meaningful_up_to_four"
    else:
        selected = [item for _, item in sorted(filtered, key=rank_key)[:2]]
        policy = "top_two_when_more_than_four"
    return {
        "eligible_count": len(filtered),
        "selected_count": len(selected),
        "policy": policy,
        "selected": selected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = select_variants(json.loads(args.input.read_text(encoding="utf-8")))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "ok", "selected_count": result["selected_count"], "output": str(args.output)}))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
