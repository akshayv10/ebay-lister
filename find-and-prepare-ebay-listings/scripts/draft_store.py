#!/usr/bin/env python3
"""Durable draft records for the review-before-publish flow.

A draft is one sourced-and-enriched product that has NOT been listed. It lives as a JSON
file under ``DRAFT_DIR`` (default ``state/drafts``), which the daily workflow already
commits and pushes, so drafts survive between Actions runs.

Nothing here touches eBay. A draft holds the normalized source (title, description,
aspects, images, variants, prices), the read-only eBay validation result, and advisory
spare images — everything a reviewer needs to decide, edit, and approve.

Status flow::

    draft ──► publishing ──► live
      │            │
      │            └──► publish_failed   (eBay refused; editable and retryable)
      ├──► blocked                       (source drifted; editable and retryable)
      └──► rejected                      (reviewer said no)

Only ``draft``, ``blocked``, and ``publish_failed`` drafts are publishable, so a live
listing can never be created twice from the same draft.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ebay_common import EbayError, read_json, write_json

DRAFT_DIR = Path(os.environ.get("DRAFT_DIR", "state/drafts"))

STATUS_DRAFT = "draft"
STATUS_PUBLISHING = "publishing"
STATUS_LIVE = "live"
STATUS_BLOCKED = "blocked"
STATUS_PUBLISH_FAILED = "publish_failed"
STATUS_REJECTED = "rejected"

# A reviewer may retry a draft that was blocked or that eBay refused; "live" is terminal
# so an approved row can never publish the same product twice.
PUBLISHABLE_STATUSES = {STATUS_DRAFT, STATUS_BLOCKED, STATUS_PUBLISH_FAILED}

_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class DraftError(EbayError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_draft_id(run_stamp: str, product_id: str) -> str:
    """Deterministic per run + product, so re-running a day's sourcing updates the same
    draft instead of piling up duplicates."""
    stamp = _ID_SAFE.sub("-", str(run_stamp or "").strip()) or "run"
    product = _ID_SAFE.sub("-", str(product_id or "").strip()) or "product"
    return f"{stamp}-{product}"


def _directory(directory: Path | None) -> Path:
    """Resolve the draft directory at call time, not at import time, so DRAFT_DIR stays
    overridable (tests, alternate runs) after this module has been imported."""
    return DRAFT_DIR if directory is None else directory


def path_for(draft_id: str, directory: Path | None = None) -> Path:
    safe = _ID_SAFE.sub("-", str(draft_id or "").strip())
    if not safe:
        raise DraftError("Draft ID must not be empty")
    return _directory(directory) / f"{safe}.json"


def new_draft(
    source: dict[str, Any],
    *,
    run_stamp: str,
    validation: dict[str, Any] | None = None,
    spare_images: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    product_id = str(source.get("product_id", "")).strip()
    if not product_id:
        raise DraftError("Cannot draft a source without a product_id")
    validation = validation or {}
    return {
        "draft_id": make_draft_id(run_stamp, product_id),
        "status": STATUS_DRAFT,
        "published": False,
        "publish_allowed": False,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "run_stamp": str(run_stamp or ""),
        "product_id": product_id,
        "source": source,
        "validation": {
            "category_id": str(validation.get("category_id", "")),
            "normalized_aspects": validation.get("normalized_aspects", {}) or {},
            "warnings": list(validation.get("warnings", []) or []),
        },
        "spare_images": list(spare_images or []),
        "notes": list(notes or []),
        "listing_id": "",
        "ebay_url": "",
        "publish_error": "",
    }


def save(draft: dict[str, Any], directory: Path | None = None) -> Path:
    draft["updated_at"] = _now_iso()
    path = path_for(draft["draft_id"], directory)
    write_json(path, draft)
    return path


def load(draft_id: str, directory: Path | None = None) -> dict[str, Any]:
    path = path_for(draft_id, directory)
    if not path.exists():
        raise DraftError(f"No draft named {draft_id}")
    return read_json(path)


def load_all(directory: Path | None = None) -> list[dict[str, Any]]:
    """Every readable draft, newest first. An unparseable file is skipped, not fatal."""
    root = _directory(directory)
    drafts: list[dict[str, Any]] = []
    if not root.exists():
        return drafts
    for path in sorted(root.glob("*.json")):
        try:
            drafts.append(read_json(path))
        except EbayError:
            continue
    drafts.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return drafts


def pending(directory: Path | None = None) -> list[dict[str, Any]]:
    return [item for item in load_all(directory) if item.get("status") in PUBLISHABLE_STATUSES]


def is_publishable(draft: dict[str, Any]) -> bool:
    return draft.get("status") in PUBLISHABLE_STATUSES and not draft.get("published")


def mark(
    draft: dict[str, Any],
    status: str,
    *,
    directory: Path | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Set a draft's status plus any result fields, and persist it."""
    draft["status"] = status
    draft["published"] = status == STATUS_LIVE
    # publish_allowed exists only for the duration of an approved publish attempt, matching
    # the convention listing_job/ebay_listing already use for prepared offers.
    draft["publish_allowed"] = status == STATUS_PUBLISHING
    draft.update(fields)
    save(draft, directory)
    return draft


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("draft_id")
    reject = sub.add_parser("reject")
    reject.add_argument("draft_id")
    args = parser.parse_args()
    try:
        if args.command == "list":
            rows = [
                {
                    "draft_id": item.get("draft_id"),
                    "status": item.get("status"),
                    "title": item.get("source", {}).get("listing_title", ""),
                    "created_at": item.get("created_at"),
                }
                for item in load_all()
            ]
            print(json.dumps(rows, indent=2))
        elif args.command == "show":
            print(json.dumps(load(args.draft_id), indent=2))
        else:
            mark(load(args.draft_id), STATUS_REJECTED)
            print(json.dumps({"status": STATUS_REJECTED, "draft_id": args.draft_id}))
        return 0
    except (EbayError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
