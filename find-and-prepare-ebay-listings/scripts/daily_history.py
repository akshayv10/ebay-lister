#!/usr/bin/env python3
"""Deterministic helpers for daily resale history and draft reconciliation."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


NICHES = [
    "Smartphone Accessories",
    "Hobbyist & Interactive Toys",
    "Home Improvements & Lighting",
    "Automotive Parts & Accessories",
    "Beauty & Self Care",
]

REQUIRED_FIELDS: dict[str, Any] = {
    "local_calendar_date": "",
    "assigned_niche": "",
    "recommendation_status": "recommended",
    "ebay_listing_status": "not_listed",
    "product_title": "",
    "functional_fingerprint": "",
    "aliexpress_url": "",
    "selected_variant": "",
    "selected_variants": [],
    "ebay_draft_id": "",
    "ebay_draft_url": "",
    "draft_saved_at": "",
    "ebay_item_number": "",
    "ebay_url": "",
    "ebay_start_displayed": "",
    "ebay_start_india": "",
    "first_seen_at": "",
    "last_confirmed_at": "",
}


def canonical_aliexpress_url(value: str) -> tuple[str, str]:
    parsed = urlparse(value.strip())
    host = parsed.netloc.casefold().split(":", 1)[0]
    if not (host == "aliexpress.us" or host.endswith(".aliexpress.us") or host == "aliexpress.com" or host.endswith(".aliexpress.com")):
        raise ValueError(f"Not an AliExpress URL: {value}")
    match = re.search(r"/item/(\d{8,20})(?:\.html)?", parsed.path, re.I)
    if not match:
        raise ValueError(f"Could not find an AliExpress product ID in: {value}")
    product_id = match.group(1)
    return product_id, f"https://www.aliexpress.us/item/{product_id}.html"


def canonical_ebay_url(value: str) -> tuple[str, str]:
    match = re.search(r"(?:/itm/|item(?:Id|Number)?[=/ :])(\d{9,15})", value, re.I)
    if not match and re.fullmatch(r"\d{9,15}", value.strip()):
        item_number = value.strip()
    elif match:
        item_number = match.group(1)
    else:
        raise ValueError(f"Could not find an eBay item number in: {value}")
    return item_number, f"https://www.ebay.com/itm/{item_number}"


def convert_ebay_start(value: str) -> dict[str, str]:
    cleaned = re.sub(r"\s+", " ", value.strip()).replace(" at ", " ")
    match = re.fullmatch(
        r"([A-Za-z]{3,9} \d{1,2}, \d{4}) (\d{1,2}:\d{2}\s*[ap]m) (PST|PDT)",
        cleaned,
        re.I,
    )
    if not match:
        raise ValueError(f"Unsupported eBay Start date: {value}")
    naive = datetime.strptime(
        f"{match.group(1)} {match.group(2).replace(' ', '')}", "%b %d, %Y %I:%M%p"
    )
    offset = timedelta(hours=-8 if match.group(3).upper() == "PST" else -7)
    source = naive.replace(tzinfo=timezone(offset, name=match.group(3).upper()))
    india = source.astimezone(ZoneInfo("Asia/Kolkata"))
    return {
        "ebay_start_displayed": value.strip(),
        "ebay_start_india": india.isoformat(),
        "local_calendar_date": india.date().isoformat(),
    }


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"History line {line_number} is not a JSON object")
        records.append(value)
    return records


def normalized_identity(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def normalize_record(record: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {**REQUIRED_FIELDS, **record}
    stamp = now or datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
    result["first_seen_at"] = result["first_seen_at"] or stamp
    result["last_confirmed_at"] = stamp
    if result["assigned_niche"] not in NICHES:
        raise ValueError(f"Unknown niche: {result['assigned_niche']}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(result["local_calendar_date"])):
        raise ValueError("local_calendar_date must be YYYY-MM-DD")
    if result.get("aliexpress_url"):
        _, result["aliexpress_url"] = canonical_aliexpress_url(str(result["aliexpress_url"]))
    item_source = str(result.get("ebay_item_number") or result.get("ebay_url") or "").strip()
    if item_source:
        item_number, url = canonical_ebay_url(item_source)
        result["ebay_item_number"] = item_number
        result["ebay_url"] = url
    displayed = str(result.get("ebay_start_displayed") or "").strip()
    if displayed:
        result["ebay_start_india"] = convert_ebay_start(displayed)["ebay_start_india"]
    return result


def same_record(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    if incoming.get("ebay_item_number") and existing.get("ebay_item_number"):
        if incoming["ebay_item_number"] == existing["ebay_item_number"]:
            return True
    if incoming.get("aliexpress_url") and existing.get("aliexpress_url"):
        if incoming["aliexpress_url"] == existing["aliexpress_url"]:
            return True
    for key in ("functional_fingerprint", "product_title"):
        left = normalized_identity(str(existing.get(key, "")))
        right = normalized_identity(str(incoming.get(key, "")))
        if left and left == right:
            return True
    return False


def upsert_history(path: Path, record: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    records = load_history(path)
    normalized = normalize_record(record, now=now)
    for index, existing in enumerate(records):
        if not same_record(existing, normalized):
            continue
        preserved = {**REQUIRED_FIELDS, **existing}
        merge_keys = set(record)
        if record.get("ebay_item_number") or record.get("ebay_url"):
            merge_keys.update(("ebay_item_number", "ebay_url"))
        if record.get("ebay_start_displayed"):
            merge_keys.update(("ebay_start_displayed", "ebay_start_india"))
        for key in merge_keys:
            if key in normalized and normalized[key] not in ("", None, []):
                preserved[key] = normalized[key]
        for key in (
            "local_calendar_date",
            "assigned_niche",
            "ebay_item_number",
            "ebay_url",
            "ebay_start_displayed",
            "ebay_start_india",
        ):
            if existing.get(key):
                preserved[key] = existing[key]
        preserved["first_seen_at"] = existing.get("first_seen_at") or normalized["first_seen_at"]
        preserved["last_confirmed_at"] = normalized["last_confirmed_at"]
        records[index] = preserved
        normalized = preserved
        break
    else:
        records.append(normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records),
        encoding="utf-8",
    )
    return normalized


def choose_niche(records: list[dict[str, Any]], today: date) -> str:
    same_day = [record for record in records if record.get("local_calendar_date") == today.isoformat()]
    if same_day and same_day[-1].get("assigned_niche") in NICHES:
        return str(same_day[-1]["assigned_niche"])
    start = today - timedelta(days=5)
    by_day: dict[date, set[str]] = {}
    for record in records:
        if record.get("ebay_listing_status") != "listed":
            continue
        try:
            record_date = date.fromisoformat(str(record.get("local_calendar_date", "")))
        except ValueError:
            continue
        niche = record.get("assigned_niche")
        if start <= record_date < today and niche in NICHES:
            by_day.setdefault(record_date, set()).add(str(niche))
    expected = [today - timedelta(days=offset) for offset in range(5, 0, -1)]
    complete = sorted(day for day, niches in by_day.items() if len(niches) == 1)
    if complete == expected:
        latest = next(iter(by_day[complete[-1]]))
        return NICHES[(NICHES.index(latest) + 1) % len(NICHES)]
    last_used: dict[str, date | None] = {niche: None for niche in NICHES}
    for day, niches in by_day.items():
        for niche in niches:
            if last_used[niche] is None or day > last_used[niche]:
                last_used[niche] = day
    return min(NICHES, key=lambda niche: (last_used[niche] or date.min, NICHES.index(niche)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    source = subparsers.add_parser("canonical-aliexpress-url")
    source.add_argument("value")
    ebay = subparsers.add_parser("canonical-ebay-url")
    ebay.add_argument("value")
    convert = subparsers.add_parser("convert-start")
    convert.add_argument("value")
    upsert = subparsers.add_parser("upsert")
    upsert.add_argument("--history", required=True, type=Path)
    upsert.add_argument("--record-json", required=True)
    upsert.add_argument("--now")
    next_niche = subparsers.add_parser("next-niche")
    next_niche.add_argument("--history", required=True, type=Path)
    next_niche.add_argument("--today", required=True)
    args = parser.parse_args()
    if args.command == "canonical-aliexpress-url":
        product_id, url = canonical_aliexpress_url(args.value)
        print(json.dumps({"product_id": product_id, "aliexpress_url": url}))
    elif args.command == "canonical-ebay-url":
        item_number, url = canonical_ebay_url(args.value)
        print(json.dumps({"ebay_item_number": item_number, "ebay_url": url}))
    elif args.command == "convert-start":
        print(json.dumps(convert_ebay_start(args.value)))
    elif args.command == "upsert":
        print(json.dumps(upsert_history(args.history, json.loads(args.record_json), args.now), ensure_ascii=False))
    else:
        print(choose_niche(load_history(args.history), date.fromisoformat(args.today)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
