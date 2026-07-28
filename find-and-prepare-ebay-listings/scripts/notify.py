#!/usr/bin/env python3
"""Email the daily run result.

Transport (pick one via environment):
  * SMTP (default): SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587),
    SMTP_USER, SMTP_PASS (a Gmail App Password), NOTIFY_FROM (default SMTP_USER).
  * SendGrid: set SENDGRID_API_KEY (and NOTIFY_FROM).
Recipient: NOTIFY_EMAIL.

Secrets are never included in the message body.
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Any


class NotifyError(RuntimeError):
    pass


def _status_prefix(status: str) -> str:
    return {"listed": "✅", "partial": "⚠️", "error": "❌"}.get(status, "ℹ️")


def compose(result: dict[str, Any]) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) from a daily-run result dict.

    Expected keys: status ('listed'|'partial'|'error'), date, niche,
    products (list of {title, aliexpress_url, ebay_url, price, ebay_price,
    listing_id}), listed_count, error (optional), notes (optional list).
    ``price`` is the AliExpress source cost; ``ebay_price`` is the actual
    eBay listing price (blank for dry runs, where nothing was listed yet)."""
    status = str(result.get("status", "error"))
    date = str(result.get("date", ""))
    niche = str(result.get("niche", ""))
    products = result.get("products", []) or []
    listed = int(result.get("listed_count", sum(1 for p in products if p.get("ebay_url"))))
    # The daily run always targets 2 products; the on-demand single-URL path sets
    # expected_count=1 so it isn't reported as "1 of 2 listed".
    expected = int(result.get("expected_count", 2))
    subject = f"{_status_prefix(status)} eBay auto-lister {date}: {listed} of {expected} listed"
    if status == "error" and not products:
        subject = f"{_status_prefix('error')} eBay auto-lister {date}: error"

    text_lines = [
        f"Daily eBay auto-lister — {date}",
        f"Niche: {niche}",
        f"Status: {status} ({listed} of {expected} listed)",
        "",
    ]
    for index, product in enumerate(products, 1):
        text_lines.append(f"Product {index}: {product.get('title', '(untitled)')}")
        if product.get("price"):
            text_lines.append(f"  Price: USD {product['price']}")
        if product.get("ebay_price"):
            text_lines.append(f"  eBay price: USD {product['ebay_price']}")
        text_lines.append(f"  AliExpress: {product.get('aliexpress_url', '(n/a)')}")
        if product.get("ebay_url"):
            text_lines.append(f"  eBay: {product['ebay_url']}")
        else:
            text_lines.append(f"  eBay: NOT LISTED — {product.get('reason', 'see error below')}")
        text_lines.append("")
    spend = result.get("spend") or {}
    spend_line = (
        f"OpenAI spend — today ${spend.get('today', 0):.4f} · "
        f"this month ${spend.get('month_to_date', 0):.4f} · "
        f"all-time ${spend.get('all_time', 0):.4f}"
    ) if spend else ""
    if spend_line:
        text_lines += [spend_line, ""]
    sheet_sync = result.get("sheet_sync") or {}
    sheet_url = str(result.get("sheet_url") or os.environ.get(
        "SHEETS_SPREADSHEET_URL",
        "https://docs.google.com/spreadsheets/d/"
        "10GgtsN_cxhHBvbEYa4vUXBUbC-LqeElkzmRiL3TT0Uk/edit",
    ))
    sheet_line = ""
    if sheet_sync:
        sheet_line = (
            f"Google Sheets: {sheet_sync.get('status', 'unknown')} "
            f"(written {sheet_sync.get('written', 0)}, queued {sheet_sync.get('queued', 0)})"
        )
        text_lines += [sheet_line, f"  {sheet_url}"]
        if sheet_sync.get("error"):
            text_lines += [f"  Sync error: {sheet_sync['error']}"]
        text_lines.append("")
    if result.get("error"):
        text_lines += ["Error:", str(result["error"]), ""]
    # Publishing one batch at a time means a backlog can build up quietly. Report it here
    # too, so the result of pressing the button says what is still waiting.
    pending = result.get("pending") or []
    parked = result.get("parked") or []
    if pending:
        text_lines += [f"{len(pending)} draft(s) still pending:"]
        text_lines += [f"  - {p.get('run_stamp', '')}  {p.get('title', '')[:60]}" for p in pending[:10]]
        text_lines += ["  Run the publish workflow again for the next batch.", ""]
    if parked:
        text_lines += [f"{len(parked)} draft(s) parked after repeated eBay refusals:"]
        text_lines += [
            f"  - {p.get('title', '')[:50]} ({p.get('attempts', 0)} attempts) — {str(p.get('error', ''))[:90]}"
            for p in parked[:10]
        ]
        text_lines.append("")
    notes = result.get("notes") or []
    if notes:
        text_lines += ["Notes:"] + [f"  - {n}" for n in notes[:20]]
    text_body = "\n".join(text_lines)

    rows = []
    for index, product in enumerate(products, 1):
        ali = html.escape(product.get("aliexpress_url", ""))
        ebay = product.get("ebay_url", "")
        ebay_cell = (
            f'<a href="{html.escape(ebay)}">{html.escape(ebay)}</a>' if ebay
            else f'<span style="color:#b00">NOT LISTED — {html.escape(str(product.get("reason", "")))}</span>'
        )
        ebay_price = product.get("ebay_price", "")
        ebay_price_cell = f"USD {html.escape(str(ebay_price))}" if ebay_price else ""
        rows.append(
            f"<tr><td>{index}</td><td>{html.escape(product.get('title',''))}</td>"
            f"<td>USD {html.escape(str(product.get('price','')))}</td>"
            f"<td>{ebay_price_cell}</td>"
            f'<td><a href="{ali}">AliExpress</a></td><td>{ebay_cell}</td></tr>'
        )
    spend_html = f"<p style='color:#555'>{html.escape(spend_line)}</p>" if spend_line else ""
    sheet_html = (
        f"<p><b>{html.escape(sheet_line)}</b><br>"
        f'<a href="{html.escape(sheet_url)}">Open Auto Lister sheet</a>'
        + (
            f"<br><span style='color:#b00'>Sync error: "
            f"{html.escape(str(sheet_sync.get('error', '')))}</span>"
            if sheet_sync.get("error") else ""
        )
        + "</p>"
    ) if sheet_sync else ""
    error_html = f"<p><b>Error:</b><br><pre>{html.escape(str(result['error']))}</pre></p>" if result.get("error") else ""
    html_body = (
        f"<h2>{_status_prefix(status)} Daily eBay auto-lister — {html.escape(date)}</h2>"
        f"<p>Niche: <b>{html.escape(niche)}</b> · Status: <b>{html.escape(status)}</b> "
        f"({listed} of 2 listed)</p>"
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<tr><th>#</th><th>Title</th><th>Price</th><th>eBay Price</th><th>Source</th><th>eBay listing</th></tr>"
        + "".join(rows)
        + "</table>"
        + spend_html
        + sheet_html
        + error_html
    )
    return subject, text_body, html_body


def compose_drafts(result: dict[str, Any]) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) for a draft run.

    The HTML body is the rendered review page itself, so the listing can be verified from
    the email without opening anything. The text part carries the same facts plus the
    two-step instruction for approving.
    """
    date = str(result.get("date", ""))
    niche = str(result.get("niche", ""))
    drafts = result.get("drafts", []) or []
    count = int(result.get("draft_count", len(drafts)))
    sheet_url = str(result.get("sheet_url") or os.environ.get("SHEETS_SPREADSHEET_URL", ""))
    subject = f"📝 eBay drafts {date}: {count} ready to review"
    if not count:
        subject = f"❌ eBay drafts {date}: nothing drafted"

    lines = [
        f"eBay listing drafts — {date}",
        f"Niche: {niche}",
        f"{count} draft(s) ready for review. NOTHING IS LIVE and nothing was created on eBay.",
        "",
    ]
    for index, draft in enumerate(drafts, 1):
        source = draft.get("source", {}) or {}
        variant = (source.get("selected_variants") or [{}])[0]
        lines.append(f"Draft {index}: {source.get('listing_title', '(untitled)')}")
        lines.append(f"  Draft ID: {draft.get('draft_id', '')}")
        lines.append(f"  Cost: USD {variant.get('delivered_total', '')}"
                     f"  →  eBay price: USD {variant.get('expected_ebay_price', '')}")
        lines.append(f"  Images: {len(source.get('source_images') or [])}"
                     f" ({len(draft.get('spare_images') or [])} spare available)")
        lines.append(f"  AliExpress: {source.get('aliexpress_url', '(n/a)')}")
        warnings = list((draft.get("validation") or {}).get("warnings") or []) + list(draft.get("notes") or [])
        for warning in warnings:
            lines.append(f"  ! {warning}")
        lines.append("")
    lines += [
        "To publish, whenever you are ready:",
        "  Actions -> 'Publish approved eBay drafts' -> Run workflow.",
        f"  That lists this batch. Status board: {sheet_url}",
        "",
    ]
    # Older drafts are deliberately not swept up by that button, so say they are there.
    older = result.get("pending_older") or []
    if older:
        lines.append(f"{len(older)} older draft(s) are still waiting:")
        for item in older[:10]:
            created = str(item.get("created_at", ""))[:10]
            parked = " [parked — eBay refused it repeatedly]" if item.get("parked") else ""
            lines.append(f"  - {created}  {item.get('title', '')[:60]}{parked}")
        lines += ["  Run the publish workflow with scope 'all' to clear them.", ""]
    sheet = result.get("draft_sheet") or {}
    if sheet.get("error"):
        lines += [f"Drafts sheet error: {sheet['error']}", ""]
    if result.get("error"):
        lines += ["Error:", str(result["error"]), ""]
    notes = result.get("notes") or []
    if notes:
        lines += ["Notes:"] + [f"  - {note}" for note in notes[:20]]
    text_body = "\n".join(lines)

    html_body = str(result.get("preview_html") or "")
    if not html_body:
        html_body = (
            f"<h2>📝 {count} eBay listing draft(s) ready — {html.escape(date)}</h2>"
            f"<pre>{html.escape(text_body)}</pre>"
        )
    return subject, text_body, html_body


def _send_smtp(subject: str, text_body: str, html_body: str, to_addr: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    from_addr = os.environ.get("NOTIFY_FROM", user).strip()
    if not user or not password:
        raise NotifyError("SMTP_USER / SMTP_PASS are not set")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = to_addr
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(message)


def _send_sendgrid(subject: str, text_body: str, html_body: str, to_addr: str) -> None:
    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    from_addr = os.environ.get("NOTIFY_FROM", "").strip()
    if not from_addr:
        raise NotifyError("NOTIFY_FROM is required for SendGrid")
    payload = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": from_addr},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html", "value": html_body},
        ],
    }
    request = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=30)
    except Exception as exc:  # noqa: BLE001
        raise NotifyError(f"SendGrid send failed: {exc}") from exc


def _deliver(subject: str, text_body: str, html_body: str) -> None:
    to_addr = os.environ.get("NOTIFY_EMAIL", "").strip()
    if not to_addr:
        raise NotifyError("NOTIFY_EMAIL is not set")
    if os.environ.get("SENDGRID_API_KEY", "").strip():
        _send_sendgrid(subject, text_body, html_body, to_addr)
    else:
        _send_smtp(subject, text_body, html_body, to_addr)


def send(result: dict[str, Any]) -> None:
    _deliver(*compose(result))


def send_drafts(result: dict[str, Any]) -> None:
    """Notify that drafts are waiting for review. Nothing has been listed."""
    _deliver(*compose_drafts(result))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", help="Path to a run-summary JSON file")
    parser.add_argument("--test", action="store_true",
                        help="Send a harmless delivery test without running the eBay pipeline.")
    parser.add_argument("--print-only", action="store_true", help="Compose and print, do not send")
    args = parser.parse_args()
    if args.test:
        result = {
            "date": "",
            "status": "listed",
            "niche": "notification test",
            "listed_count": 0,
            "products": [],
            "notes": ["This is a delivery test. No eBay listing was created."],
        }
    elif args.result:
        with open(args.result, encoding="utf-8") as handle:
            result = json.load(handle)
    else:
        parser.error("one of --result or --test is required")
    subject, text_body, html_body = compose(result)
    if args.print_only:
        print(subject)
        print()
        print(text_body)
        return 0
    send(result)
    print(json.dumps({"status": "sent", "subject": subject}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
