#!/usr/bin/env python3
"""Running ledger of OpenAI API spend.

Every call appends one JSON line to ``state/openai-spend.jsonl`` (persisted by the
workflow's existing state commit-back), so the daily email can report today's,
this month's, and all-time spend.

Override the ledger location with SPEND_LEDGER.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# USD per 1M tokens: (input, output). Update if OpenAI pricing changes.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}
DEFAULT_PRICE = (0.40, 1.60)

LEDGER = Path(os.environ.get("SPEND_LEDGER", "state/openai-spend.jsonl"))


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost in USD for one call, using the model's per-1M-token pricing."""
    price_in, price_out = PRICES.get((model or "").strip(), DEFAULT_PRICE)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def record(model: str, input_tokens: int, output_tokens: int, purpose: str = "") -> float:
    """Append a usage line to the ledger and return this call's cost."""
    amount = cost_usd(model, input_tokens, output_tokens)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": date.today().isoformat(),
        "model": model,
        "purpose": purpose,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cost_usd": round(amount, 6),
    }
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass  # never let bookkeeping break a run
    return amount


def _entries() -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    out.append(value)
    except OSError:
        return []
    return out


def totals() -> dict[str, float]:
    """Spend rolled up as {'today', 'month_to_date', 'all_time'} in USD."""
    today = date.today().isoformat()
    month = today[:7]
    sums = {"today": 0.0, "month_to_date": 0.0, "all_time": 0.0}
    for entry in _entries():
        amount = float(entry.get("cost_usd", 0) or 0)
        stamp = str(entry.get("date", ""))
        sums["all_time"] += amount
        if stamp.startswith(month):
            sums["month_to_date"] += amount
        if stamp == today:
            sums["today"] += amount
    return {key: round(value, 4) for key, value in sums.items()}


def summary_line() -> str:
    t = totals()
    return (
        f"OpenAI spend — today ${t['today']:.4f} · "
        f"this month ${t['month_to_date']:.4f} · all-time ${t['all_time']:.4f}"
    )


if __name__ == "__main__":
    print(summary_line())
