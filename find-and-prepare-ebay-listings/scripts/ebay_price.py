#!/usr/bin/env python3
"""Quote eBay prices with the supplied extension's fee and rounding model."""

from __future__ import annotations

import argparse
import json
import math
from decimal import Decimal, ROUND_HALF_UP


def money(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_up_destination_price(value: float) -> float:
    amount = max(0.0, float(value))
    dollars = math.floor(amount)
    if abs(amount - dollars) < 1e-9 and str(dollars).endswith(("0", "5", "9")):
        return float(dollars)
    if amount > dollars and str(dollars).endswith("9"):
        return float(math.ceil(amount))
    candidate = dollars
    while candidate < amount or not str(candidate).endswith(("5", "9")):
        candidate += 1
    return float(candidate)


def estimated_margin(price: float, cost: float, fvf_rate: float, ad_rate: float, order_fee: float) -> float:
    if price <= 0:
        return 0.0
    return (price - cost - price * (fvf_rate + ad_rate) - order_fee) / price


def quote(
    cost: float,
    target_margin: float = 0.50,
    final_value_fee: float = 0.1325,
    promoted_rate: float = 0.10,
    order_fee: float = 0.30,
) -> dict[str, float | str]:
    if cost <= 0:
        raise ValueError("cost must be positive")
    rates = (target_margin, final_value_fee, promoted_rate)
    if any(rate < 0 or rate >= 1 for rate in rates):
        raise ValueError("percentage rates must be decimals from 0 up to but not including 1")
    denominator = 1 - target_margin - final_value_fee - promoted_rate
    if denominator <= 0:
        raise ValueError("fee and margin settings leave no viable sale-price denominator")
    price = round_up_destination_price((cost + order_fee) / denominator)
    while estimated_margin(price, cost, final_value_fee, promoted_rate, order_fee) + 1e-9 < target_margin:
        price = round_up_destination_price(price + 0.01)
    fvf_amount = money(price * final_value_fee)
    promoted_amount = money(price * promoted_rate)
    profit = money(price - cost - fvf_amount - promoted_amount - order_fee)
    margin = money((profit / price) * 100)
    return {
        "currency": "USD",
        "delivered_cost": money(cost),
        "suggested_price": money(price),
        "target_margin_percent": money(target_margin * 100),
        "final_value_fee_percent": money(final_value_fee * 100),
        "promoted_rate_percent": money(promoted_rate * 100),
        "order_fee": money(order_fee),
        "estimated_final_value_fee": fvf_amount,
        "estimated_promoted_fee": promoted_amount,
        "estimated_profit": profit,
        "estimated_margin_percent": margin,
        "rounding": "whole dollar ending in 5 or 9, with decimal 9-ending values advancing to the next 0",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cost", required=True, type=float)
    parser.add_argument("--target-margin", type=float, default=0.50)
    parser.add_argument("--final-value-fee", type=float, default=0.1325)
    parser.add_argument("--promoted-rate", type=float, default=0.10)
    parser.add_argument("--order-fee", type=float, default=0.30)
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        result = quote(args.cost, args.target_margin, args.final_value_fee, args.promoted_rate, args.order_fee)
        payload = json.dumps(result, indent=2) + "\n"
        if args.output:
            from pathlib import Path

            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload, encoding="utf-8")
        print(payload, end="")
        return 0
    except ValueError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
