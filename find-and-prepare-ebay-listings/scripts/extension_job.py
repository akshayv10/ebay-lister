#!/usr/bin/env python3
"""Record extension-created eBay forms, validate final audits, and build handoff reports."""

from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from candidate_ledger import summarize
from ebay_price import quote


class JobError(ValueError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JobError(f"Could not read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise JobError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def require_text(value: dict[str, Any], key: str) -> str:
    result = str(value.get(key, "")).strip()
    if not result:
        raise JobError(f"Missing required field: {key}")
    return result


def decimal_value(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise JobError(f"{field} must be decimal money") from exc
    if not result.is_finite():
        raise JobError(f"{field} must be finite")
    return result


def canonical_source_url(value: str) -> str:
    parsed = urlparse(value.strip())
    host = parsed.netloc.casefold().split(":", 1)[0]
    if parsed.scheme != "https" or not (
        host == "aliexpress.us" or host.endswith(".aliexpress.us")
        or host == "aliexpress.com" or host.endswith(".aliexpress.com")
    ):
        raise JobError("aliexpress_url must be an HTTPS AliExpress product URL")
    match = re.search(r"/item/(\d{8,20})(?:\.html)?", parsed.path, re.I)
    if not match:
        raise JobError("aliexpress_url does not contain a product ID")
    return f"https://www.aliexpress.us/item/{match.group(1)}.html"


def ebay_form_url(value: str) -> str:
    parsed = urlparse(value.strip())
    host = parsed.netloc.casefold().split(":", 1)[0]
    if parsed.scheme != "https" or not (host == "ebay.com" or host.endswith(".ebay.com")):
        raise JobError("form_url must be an HTTPS eBay URL")
    return value.strip()


def extension_state_action(
    state: str | None,
    *,
    button_present: bool = True,
    clicked_once: bool = False,
    observed_after_click: bool = False,
    timed_out: bool = False,
    reloaded_stale_once: bool = False,
) -> dict[str, Any]:
    """Return the only safe next action for one observed button state."""
    if not button_present:
        return {"action": "stop", "reason": "missing_button", "retry_click": False}
    if timed_out:
        return {"action": "stop", "reason": "extension_timeout", "retry_click": False}
    normalized = str(state or "").strip().casefold()
    if normalized not in {"idle", "running", "done", "error", "busy"}:
        return {"action": "stop", "reason": "unknown_state", "retry_click": False}
    if clicked_once:
        if normalized == "running":
            return {"action": "wait", "reason": "running", "retry_click": False}
        if normalized == "done" and observed_after_click:
            return {"action": "bind_ebay_form", "reason": "done", "retry_click": False}
        reason = {
            "error": "extension_error",
            "busy": "ownership_lost",
            "idle": "unexpected_idle_after_click",
            "done": "stale_done",
        }[normalized]
        return {"action": "stop", "reason": reason, "retry_click": False}
    if normalized == "idle":
        return {"action": "click_once", "reason": "idle", "retry_click": False}
    if normalized in {"running", "busy"}:
        return {"action": "wait", "reason": normalized, "retry_click": False}
    if reloaded_stale_once:
        return {"action": "stop", "reason": f"stale_{normalized}", "retry_click": False}
    return {"action": "reload_once", "reason": f"stale_{normalized}", "retry_click": False}


def normalize_source(source: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in (
        "run_id", "local_calendar_date", "assigned_niche", "product_id",
        "source_title", "functional_fingerprint", "verified_brand",
    ):
        normalized[key] = require_text(source, key)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized["local_calendar_date"]):
        raise JobError("local_calendar_date must be YYYY-MM-DD")
    normalized["aliexpress_url"] = canonical_source_url(require_text(source, "aliexpress_url"))
    if normalized["product_id"] not in normalized["aliexpress_url"]:
        raise JobError("product_id must match aliexpress_url")
    variants = source.get("selected_variants")
    if not isinstance(variants, list) or not 1 <= len(variants) <= 4:
        raise JobError("selected_variants must contain 1 to 4 combinations")
    seen: set[str] = set()
    normalized_variants: list[dict[str, Any]] = []
    for index, item in enumerate(variants, 1):
        if not isinstance(item, dict):
            raise JobError(f"selected_variants[{index}] must be an object")
        variant_id = require_text(item, "id")
        if variant_id in seen:
            raise JobError(f"Duplicate variant id: {variant_id}")
        seen.add(variant_id)
        options = item.get("options", {})
        if not isinstance(options, dict):
            raise JobError(f"Variant {variant_id} options must be an object")
        visible = decimal_value(item.get("visible_item_price"), f"{variant_id}.visible_item_price")
        delivered = decimal_value(item.get("delivered_total"), f"{variant_id}.delivered_total")
        if visible < Decimal("15.00"):
            raise JobError(f"Variant {variant_id} visible item price is below USD 15")
        if delivered <= Decimal("0.00"):
            raise JobError(f"Variant {variant_id} delivered total must be positive for pricing")
        if int(item.get("quantity", 1)) != 1:
            raise JobError(f"Variant {variant_id} quantity must be 1")
        expected = Decimal(str(quote(float(delivered))["suggested_price"]))
        normalized_variants.append({
            "id": variant_id,
            "options": {str(key): str(value) for key, value in options.items()},
            "visible_item_price": f"{visible:.2f}",
            "delivered_total": f"{delivered:.2f}",
            "expected_ebay_price": f"{expected:.2f}",
            "quantity": 1,
        })
    normalized["selected_variants"] = normalized_variants
    return normalized


def initialize_result(source_path: Path, result_path: Path) -> dict[str, Any]:
    source = normalize_source(read_json(source_path))
    result = {
        **source,
        "status": "accepted",
        "extension_handoff": {
            "state": "pending", "message": "", "clicked_once": False,
            "observed_after_click": False, "source_tab_id": None,
            "ebay_tab_id": None, "form_url": None,
        },
        "form_ready": False,
        "draft_saved": False,
        "published": False,
        "publish_allowed": False,
        "verification_checkpoints": [],
        "form_attempts": [],
        "final_audit": None,
    }
    write_json(result_path, result)
    return result


def record_extension(
    result_path: Path,
    source_tab_id: str,
    ebay_tab_id: str,
    form_url: str,
    state: str,
    message: str,
    clicked_once: bool,
    observed_after_click: bool,
) -> dict[str, Any]:
    result = read_json(result_path)
    if result.get("status") != "accepted":
        raise JobError("Extension handoff can be recorded only once from accepted state")
    decision = extension_state_action(
        state, clicked_once=clicked_once, observed_after_click=observed_after_click
    )
    if decision["action"] != "bind_ebay_form":
        raise JobError(f"Extension handoff is not a fresh done state: {decision['reason']}")
    source_tab_id = source_tab_id.strip()
    ebay_tab_id = ebay_tab_id.strip()
    if not source_tab_id or not ebay_tab_id:
        raise JobError("Both source_tab_id and ebay_tab_id are required")
    if source_tab_id == ebay_tab_id:
        raise JobError("Source and eBay tabs must be distinct")
    message = message.strip()
    if not message:
        raise JobError("Extension terminal message is required")
    result["status"] = "extension_done"
    result["extension_handoff"] = {
        "state": "done",
        "message": message,
        "clicked_once": True,
        "observed_after_click": True,
        "source_tab_id": source_tab_id,
        "ebay_tab_id": ebay_tab_id,
        "form_url": ebay_form_url(form_url),
    }
    write_json(result_path, result)
    return result


def expected_prices(result: dict[str, Any]) -> dict[str, Decimal]:
    return {
        require_text(item, "id"): decimal_value(item.get("expected_ebay_price"), "expected_ebay_price")
        for item in result.get("selected_variants", []) if isinstance(item, dict)
    }


def validate_final_audit(result: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") != "extension_done":
        raise JobError("Final audit requires extension state done")
    handoff = result.get("extension_handoff", {})
    if handoff.get("state") != "done" or handoff.get("clicked_once") is not True:
        raise JobError("Final audit requires a fresh recorded extension handoff")
    normalized: dict[str, Any] = {}
    for key in ("title", "category", "condition", "description", "item_location"):
        normalized[key] = require_text(audit, key)
    if len(normalized["title"]) > 80:
        raise JobError("Final title exceeds 80 characters")
    image_count = int(audit.get("image_count", 0))
    video_count = int(audit.get("video_count", 0))
    if image_count < 1:
        raise JobError("Final form requires at least one image")
    if video_count < 0:
        raise JobError("video_count cannot be negative")
    if audit.get("media_settled") is not True:
        raise JobError("Extension media upload is not settled")
    if int(audit.get("image_upload_failures", 0)) != 0:
        raise JobError("Extension media contains upload failures")
    if int(audit.get("quantity", 0)) != 1:
        raise JobError("Final quantity must be 1")
    expected_ids = list(expected_prices(result))
    variation_ids = audit.get("variation_ids")
    if not isinstance(variation_ids, list) or [str(value) for value in variation_ids] != expected_ids:
        raise JobError("Final variations must exactly match selected source combinations")
    brand = require_text(audit, "brand")
    if brand.casefold() != str(result.get("verified_brand", "")).strip().casefold():
        raise JobError("Final Brand does not match verified source Brand")
    if audit.get("required_item_specifics_complete") is not True:
        raise JobError("Required item specifics are incomplete")
    unfinished = audit.get("unfinished_fields")
    if not isinstance(unfinished, list) or unfinished:
        raise JobError("Final audit must contain no unfinished required fields")
    if audit.get("free_buyer_shipping") is not True:
        raise JobError("Buyer shipping must be free")
    if audit.get("account_policies_selected") is not True:
        raise JobError("Existing account policies must be selected")
    if audit.get("save_or_publish_clicked") is not False:
        raise JobError("No save or publish action may be clicked")
    observed = audit.get("observed_prices")
    if not isinstance(observed, dict) or set(observed) != set(expected_ids):
        raise JobError("Observed prices must include every expected variation exactly once")
    normalized_prices: dict[str, str] = {}
    for variant_id, expected in expected_prices(result).items():
        value = decimal_value(observed.get(variant_id), f"{variant_id}.observed_price")
        if value != expected:
            raise JobError(f"Live-form price mismatch for variation {variant_id}")
        normalized_prices[variant_id] = f"{value:.2f}"
    if audit.get("general_promotion_enabled") is not True:
        raise JobError("Promoted Listings General must be enabled")
    if decimal_value(audit.get("promoted_rate_percent"), "promoted_rate_percent") != Decimal("10.00"):
        raise JobError("Promoted Listings General rate must be exactly 10%")
    if audit.get("priority_promotion_enabled") is not False:
        raise JobError("Priority promotion must remain disabled")
    normalized.update({
        "image_count": image_count,
        "video_count": video_count,
        "media_settled": True,
        "image_upload_failures": 0,
        "quantity": 1,
        "variation_ids": expected_ids,
        "brand": brand,
        "required_item_specifics_complete": True,
        "unfinished_fields": [],
        "free_buyer_shipping": True,
        "account_policies_selected": True,
        "observed_prices": normalized_prices,
        "general_promotion_enabled": True,
        "promoted_rate_percent": "10.00",
        "priority_promotion_enabled": False,
        "save_or_publish_clicked": False,
    })
    return normalized


def record_checkpoint(
    result_path: Path,
    outcome: str,
    audit_path: Path | None = None,
    failure_fingerprint: str = "",
) -> dict[str, Any]:
    if outcome not in {"pass", "fail"}:
        raise JobError("Checkpoint outcome must be pass or fail")
    result = read_json(result_path)
    if result.get("status") == "blocked":
        raise JobError("Result is blocked after three identical form failures")
    checkpoints = result.setdefault("verification_checkpoints", [])
    attempts = result.setdefault("form_attempts", [])
    if outcome == "pass":
        if audit_path is None:
            raise JobError("Passing checkpoint requires --audit")
        audit = validate_final_audit(result, read_json(audit_path))
        checkpoint = {"stage": "final", "phase": "pre_submit", "outcome": "pass", "audit": audit}
        result["final_audit"] = audit
    else:
        fingerprint = failure_fingerprint.strip()
        if not fingerprint:
            raise JobError("Failed checkpoint requires a failure fingerprint")
        same_count = 1 + sum(
            1 for item in attempts
            if isinstance(item, dict) and item.get("failure_fingerprint") == fingerprint
        )
        attempt = {
            "stage": "final", "phase": "pre_submit", "failure_fingerprint": fingerprint,
            "same_failure_count": same_count,
        }
        attempts.append(attempt)
        checkpoint = {**attempt, "outcome": "fail"}
        if same_count >= 3:
            result.update({"status": "blocked", "blocked": True, "blocked_reason": fingerprint})
            checkpoint["phase"] = "blocked"
    checkpoints.append(checkpoint)
    write_json(result_path, result)
    return result


def latest_pass(result: dict[str, Any]) -> dict[str, Any] | None:
    for item in reversed(result.get("verification_checkpoints", [])):
        if isinstance(item, dict) and item.get("stage") == "final" and item.get("phase") == "pre_submit" and item.get("outcome") == "pass":
            return item
    return None


def record_ready(result_path: Path) -> dict[str, Any]:
    result = read_json(result_path)
    if result.get("status") == "blocked":
        raise JobError("Blocked result cannot be ready")
    if result.get("status") != "extension_done" or result.get("extension_handoff", {}).get("state") != "done":
        raise JobError("Ready form requires extension state done")
    checkpoint = latest_pass(result)
    if checkpoint is None or not isinstance(result.get("final_audit"), dict):
        raise JobError("Ready form requires a passing final pre_submit checkpoint")
    result.update({
        "status": "ready_for_user",
        "form_ready": True,
        "draft_saved": False,
        "published": False,
        "publish_allowed": False,
        "verified_form_prices": result["final_audit"]["observed_prices"],
    })
    write_json(result_path, result)
    return result


def build_report(result_paths: list[Path], ledger_path: Path, output: Path) -> dict[str, Any]:
    if len(result_paths) != 2:
        raise JobError("Exactly two result files are required")
    products: list[dict[str, Any]] = []
    source_urls: set[str] = set()
    source_tabs: set[str] = set()
    ebay_tabs: set[str] = set()
    for path in result_paths:
        result = read_json(path)
        if result.get("status") != "ready_for_user" or result.get("form_ready") is not True:
            raise JobError(f"Result is not ready_for_user: {path}")
        if result.get("draft_saved") is not False or result.get("published") is not False or result.get("publish_allowed") is not False:
            raise JobError(f"Ready form must remain unsaved and unpublished: {path}")
        handoff = result.get("extension_handoff", {})
        if handoff.get("state") != "done":
            raise JobError(f"Result lacks extension done state: {path}")
        audit = result.get("final_audit")
        if not isinstance(audit, dict):
            raise JobError(f"Result lacks a complete final audit: {path}")
        audit_source = dict(result)
        audit_source["status"] = "extension_done"
        normalized_audit = validate_final_audit(audit_source, audit)
        if result.get("verified_form_prices") != normalized_audit["observed_prices"]:
            raise JobError(f"Result lacks verified final-form prices: {path}")
        source_url = canonical_source_url(require_text(result, "aliexpress_url"))
        source_tab = require_text(handoff, "source_tab_id")
        ebay_tab = require_text(handoff, "ebay_tab_id")
        if source_url in source_urls or source_tab in source_tabs or ebay_tab in ebay_tabs:
            raise JobError("Sources and paired source/eBay tabs must be unique")
        source_urls.add(source_url)
        source_tabs.add(source_tab)
        ebay_tabs.add(ebay_tab)
        products.append({
            "product_title": result.get("source_title", ""),
            "product_id": result.get("product_id", ""),
            "aliexpress_url": source_url,
            "source_tab_id": source_tab,
            "ebay_tab_id": ebay_tab,
            "form_url": ebay_form_url(require_text(handoff, "form_url")),
            "extension_message": str(handoff.get("message", "")),
            "selected_variants": result.get("selected_variants", []),
            "verified_form_prices": normalized_audit["observed_prices"],
            "image_count": normalized_audit["image_count"],
            "video_count": normalized_audit["video_count"],
            "general_promotion_enabled": True,
            "promoted_rate_percent": "10.00",
            "priority_promotion_enabled": False,
            "unfinished_fields": [],
        })
    ledger = summarize(ledger_path)
    if ledger["accepted_count"] < 2 or ledger["search_batch_count"] < 1:
        raise JobError("Candidate ledger requires two accepted candidates and one search batch")
    payload = {"status": "ready_for_user", "ready_form_count": 2, "products": products, "candidate_ledger": ledger}
    write_json(output, payload)
    return payload


def boolean(value: str) -> bool:
    return value == "true"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--source", required=True, type=Path)
    init.add_argument("--result", required=True, type=Path)
    extension = sub.add_parser("record-extension")
    extension.add_argument("--result", required=True, type=Path)
    extension.add_argument("--source-tab-id", required=True)
    extension.add_argument("--ebay-tab-id", required=True)
    extension.add_argument("--form-url", required=True)
    extension.add_argument("--state", required=True)
    extension.add_argument("--message", default="")
    extension.add_argument("--clicked-once", required=True, choices=("true", "false"))
    extension.add_argument("--observed-after-click", required=True, choices=("true", "false"))
    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--result", required=True, type=Path)
    checkpoint.add_argument("--outcome", required=True, choices=("pass", "fail"))
    checkpoint.add_argument("--audit", type=Path)
    checkpoint.add_argument("--failure-fingerprint", default="")
    ready = sub.add_parser("record-ready")
    ready.add_argument("--result", required=True, type=Path)
    report = sub.add_parser("report")
    report.add_argument("--result", required=True, action="append", type=Path)
    report.add_argument("--ledger", required=True, type=Path)
    report.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "init":
            payload = initialize_result(args.source, args.result)
        elif args.command == "record-extension":
            payload = record_extension(
                args.result, args.source_tab_id, args.ebay_tab_id, args.form_url,
                args.state, args.message, boolean(args.clicked_once), boolean(args.observed_after_click),
            )
        elif args.command == "checkpoint":
            payload = record_checkpoint(args.result, args.outcome, args.audit, args.failure_fingerprint)
        elif args.command == "record-ready":
            payload = record_ready(args.result)
        else:
            payload = build_report(args.result, args.ledger, args.output)
        print(json.dumps({"status": payload.get("status", "ok")}))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
