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

# A draft that was blocked or that eBay refused may be retried; "live" is terminal so the
# same product can never be published twice.
PUBLISHABLE_STATUSES = {STATUS_DRAFT, STATUS_BLOCKED, STATUS_PUBLISH_FAILED}

# After this many failed publishes a draft stops being picked up automatically. Some
# products eBay simply will not accept — a branded medical device listed as Unbranded,
# say — and without this they would be retried, and fail, on every single button press.
MAX_PUBLISH_ATTEMPTS = int(os.environ.get("DRAFT_MAX_PUBLISH_ATTEMPTS", "2"))

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
        "publish_attempts": 0,
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
    """True if this draft may still be listed at all — including by an explicit retry."""
    return draft.get("status") in PUBLISHABLE_STATUSES and not draft.get("published")


def attempts(draft: dict[str, Any]) -> int:
    try:
        return int(draft.get("publish_attempts", 0) or 0)
    except (TypeError, ValueError):
        return 0


def is_parked(draft: dict[str, Any]) -> bool:
    """True once eBay has refused this draft enough times to stop auto-retrying it."""
    return is_publishable(draft) and attempts(draft) >= MAX_PUBLISH_ATTEMPTS


def auto_selectable(draft: dict[str, Any]) -> bool:
    """Publishable *and* not parked — what a plain button press should pick up."""
    return is_publishable(draft) and not is_parked(draft)


def batches(directory: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Pending drafts grouped by the run that produced them, newest run first."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for draft in pending(directory):
        grouped.setdefault(str(draft.get("run_stamp", "")), []).append(draft)
    return dict(sorted(grouped.items(), key=lambda item: item[0], reverse=True))


def latest_batch(directory: Path | None = None) -> tuple[str, list[dict[str, Any]]]:
    """The newest run's still-publishable drafts, skipping parked ones.

    Returns ("", []) when every recent draft is live or parked. Older runs are left
    alone deliberately: the button publishes today's batch, and the backlog is
    reported rather than swept up silently.
    """
    for run_stamp, drafts in batches(directory).items():
        selectable = [draft for draft in drafts if auto_selectable(draft)]
        if selectable:
            return run_stamp, selectable
    return "", []


def backlog(directory: Path | None = None, exclude_run_stamp: str = "") -> list[dict[str, Any]]:
    """Publishable drafts outside the batch being published — what to report."""
    return [
        draft for draft in pending(directory)
        if str(draft.get("run_stamp", "")) != exclude_run_stamp
    ]


def history_views(directory: Path | None = None) -> list[dict[str, Any]]:
    """Every drafted product, shaped like a history record for de-duplication.

    Sourcing de-duplicates against listing history (``daily_history.same_record``), which
    only knows about products that actually went live. A product sitting in review is
    invisible to it, so the next day's run would happily draft the same product again —
    and both drafts would be independently publishable, producing two eBay listings for
    one product. Feeding these views into the exclusion set closes that.

    Every draft counts, whatever its status: a live one is already in history (harmless
    overlap), and a blocked or rejected one is precisely something not to re-source.
    """
    views: list[dict[str, Any]] = []
    for draft in load_all(directory):
        source = draft.get("source", {}) or {}
        url = str(source.get("aliexpress_url", "")).strip()
        title = str(source.get("source_title") or source.get("listing_title") or "").strip()
        if not url and not title:
            continue
        views.append({
            "aliexpress_url": url,
            "functional_fingerprint": str(source.get("functional_fingerprint", "")).strip(),
            "product_title": title,
        })
    return views


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
