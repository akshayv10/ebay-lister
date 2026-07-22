#!/usr/bin/env python3
"""Append and summarize candidate and search-batch decisions without repeats."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from daily_history import canonical_aliexpress_url


class LedgerError(ValueError):
    pass


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"Invalid candidate ledger line {number}: {exc}") from exc
        if not isinstance(record, dict):
            raise LedgerError(f"Candidate ledger line {number} is not an object")
        records.append(record)
    return records


def append_record(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    records = load_records(path)
    record_type = record.get("record_type")
    if record_type == "candidate":
        _, key = canonical_aliexpress_url(str(record.get("canonical_url", "")))
        record["canonical_url"] = key
        if any(item.get("record_type") == "candidate" and item.get("canonical_url") == key for item in records):
            return {"status": "duplicate", "record_type": "candidate", "key": key}
    elif record_type == "search_batch":
        key = str(record.get("batch_key", "")).strip()
        if not key:
            raise LedgerError("batch_key is required")
        if any(item.get("record_type") == "search_batch" and item.get("batch_key") == key for item in records):
            return {"status": "duplicate", "record_type": "search_batch", "key": key}
    else:
        raise LedgerError("record_type must be candidate or search_batch")
    for field in ("query_or_subcategory", "status", "reason"):
        if not str(record.get(field, "")).strip():
            raise LedgerError(f"{field} is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"status": "recorded", "record_type": record_type, "key": key}


def summarize(path: Path) -> dict[str, int]:
    records = load_records(path)
    candidates = [item for item in records if item.get("record_type") == "candidate"]
    batches = [item for item in records if item.get("record_type") == "search_batch"]
    return {
        "candidate_count": len(candidates),
        "accepted_count": sum(item.get("status") == "accepted" for item in candidates),
        "rejected_count": sum(item.get("status") == "rejected" for item in candidates),
        "search_batch_count": len(batches),
        "productive_batch_count": sum(item.get("status") == "productive" for item in batches),
        "exhausted_batch_count": sum(item.get("status") == "exhausted" for item in batches),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    candidate = subparsers.add_parser("candidate")
    candidate.add_argument("--ledger", required=True, type=Path)
    candidate.add_argument("--url", required=True)
    candidate.add_argument("--query", required=True)
    candidate.add_argument("--gate", required=True)
    candidate.add_argument("--status", required=True, choices=("accepted", "rejected"))
    candidate.add_argument("--reason", required=True)
    batch = subparsers.add_parser("batch")
    batch.add_argument("--ledger", required=True, type=Path)
    batch.add_argument("--batch-key", required=True)
    batch.add_argument("--query", required=True)
    batch.add_argument("--status", required=True, choices=("productive", "exhausted"))
    batch.add_argument("--reason", required=True)
    summary = subparsers.add_parser("summary")
    summary.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "candidate":
            payload = append_record(args.ledger, {
                "record_type": "candidate", "canonical_url": args.url,
                "query_or_subcategory": args.query, "gate_reached": args.gate,
                "status": args.status, "reason": args.reason,
            })
        elif args.command == "batch":
            payload = append_record(args.ledger, {
                "record_type": "search_batch", "batch_key": args.batch_key,
                "query_or_subcategory": args.query, "status": args.status,
                "reason": args.reason,
            })
        else:
            payload = {"status": "complete", **summarize(args.ledger)}
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
