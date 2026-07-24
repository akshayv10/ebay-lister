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
    products (list of {title, aliexpress_url, ebay_url, price, listing_id}),
    listed_count, error (optional), notes (optional list)."""
    status = str(result.get("status", "error"))
    date = str(result.get("date", ""))
    niche = str(result.get("niche", ""))
    products = result.get("products", []) or []
    listed = int(result.get("listed_count", sum(1 for p in products if p.get("ebay_url"))))
    subject = f"{_status_prefix(status)} eBay auto-lister {date}: {listed} of 2 listed"
    if status == "error" and not products:
        subject = f"{_status_prefix('error')} eBay auto-lister {date}: error"

    text_lines = [
        f"Daily eBay auto-lister — {date}",
        f"Niche: {niche}",
        f"Status: {status} ({listed} of 2 listed)",
        "",
    ]
    for index, product in enumerate(products, 1):
        text_lines.append(f"Product {index}: {product.get('title', '(untitled)')}")
        if product.get("price"):
            text_lines.append(f"  Price: USD {product['price']}")
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
        rows.append(
            f"<tr><td>{index}</td><td>{html.escape(product.get('title',''))}</td>"
            f"<td>USD {html.escape(str(product.get('price','')))}</td>"
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
        "<tr><th>#</th><th>Title</th><th>Price</th><th>Source</th><th>eBay listing</th></tr>"
        + "".join(rows)
        + "</table>"
        + spend_html
        + sheet_html
        + error_html
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


def send(result: dict[str, Any]) -> None:
    to_addr = os.environ.get("NOTIFY_EMAIL", "").strip()
    if not to_addr:
        raise NotifyError("NOTIFY_EMAIL is not set")
    subject, text_body, html_body = compose(result)
    if os.environ.get("SENDGRID_API_KEY", "").strip():
        _send_sendgrid(subject, text_body, html_body, to_addr)
    else:
        _send_smtp(subject, text_body, html_body, to_addr)


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
