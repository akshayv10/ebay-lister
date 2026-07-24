#!/usr/bin/env python3
"""Poll a Gmail inbox for "list this link" requests and hand each to list_from_url.

When you find a product you want to list, email yourself (from the account the lister
already notifies) with a subject like ``LIST: https://www.aliexpress.us/item/<id>.html``.
A scheduled run of this poller reads unseen messages, extracts the first AliExpress URL,
lists it (dry-run unless ``--live``), replies with the eBay link, and marks the message
read so it is not processed twice.

Safety:
  * Only messages whose From address matches an authorized sender are acted on
    (defaults to NOTIFY_EMAIL / NOTIFY_FROM / SMTP_USER — i.e. you). Random inbound
    mail can never trigger a listing.
  * Only messages whose subject starts with INBOX_SUBJECT_TAG (default ``LIST:``).
  * The existing dedup history (state/resale-product-history.jsonl) plus the \\Seen flag
    guard against double-listing.

Run:
    python inbox_poll.py           # dry run: read + validate + reply "would list", no publish
    python inbox_poll.py --live    # actually publish each requested link

Environment (reuses the lister's Gmail app password):
    SMTP_USER / SMTP_PASS   Gmail address + app password (also used for IMAP login)
    IMAP_HOST (default imap.gmail.com), IMAP_PORT (default 993)
    INBOX_SUBJECT_TAG (default "LIST:")
    INBOX_ALLOWED_SENDERS  comma-separated override; defaults to NOTIFY_EMAIL/NOTIFY_FROM/SMTP_USER
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import sys
from email.header import decode_header, make_header
from email.message import Message
from typing import Any

import ali_api
import list_from_url

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
SUBJECT_TAG = os.environ.get("INBOX_SUBJECT_TAG", "LIST:").strip()
MAILBOX = os.environ.get("INBOX_MAILBOX", "INBOX")

# Match any http(s) URL; hosts are filtered by ali_api.product_id_from_url downstream.
_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)


def _allowed_senders() -> set[str]:
    override = os.environ.get("INBOX_ALLOWED_SENDERS", "")
    if override.strip():
        raw = override.split(",")
    else:
        raw = [os.environ.get("NOTIFY_EMAIL", ""), os.environ.get("NOTIFY_FROM", ""),
               os.environ.get("SMTP_USER", "")]
    return {a.strip().lower() for a in raw if a.strip()}


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _from_address(msg: Message) -> str:
    raw = _decode(msg.get("From", ""))
    match = re.search(r"<([^>]+)>", raw)
    return (match.group(1) if match else raw).strip().lower()


def _text_body(msg: Message) -> str:
    if not msg.is_multipart():
        try:
            return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
        except Exception:  # noqa: BLE001
            return msg.get_payload() or ""
    parts: list[str] = []
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
            try:
                parts.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue
    return "\n".join(parts)


def _first_ali_url(*texts: str) -> str | None:
    for text in texts:
        for candidate in _URL_RE.findall(text or ""):
            candidate = candidate.rstrip(".,);]>")
            try:
                ali_api.product_id_from_url(candidate)
            except ali_api.AliError:
                continue
            return candidate
    return None


def poll(live: bool) -> dict[str, Any]:
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    if not user or not password:
        raise RuntimeError("SMTP_USER / SMTP_PASS are not set (used for IMAP login)")

    allowed = _allowed_senders()
    outcome: dict[str, Any] = {"processed": [], "skipped": [], "live": live}

    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        imap.login(user, password)
        imap.select(MAILBOX)
        status, data = imap.search(None, "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        ids = data[0].split()
        for num in ids:
            fetch_status, fetch_data = imap.fetch(num, "(RFC822)")
            if fetch_status != "OK" or not fetch_data or not isinstance(fetch_data[0], tuple):
                continue
            msg = email.message_from_bytes(fetch_data[0][1])
            subject = _decode(msg.get("Subject", ""))
            sender = _from_address(msg)

            if SUBJECT_TAG and not subject.strip().upper().startswith(SUBJECT_TAG.upper()):
                continue  # not a listing request; leave unread
            if allowed and sender not in allowed:
                outcome["skipped"].append({"from": sender, "subject": subject, "reason": "unauthorized sender"})
                # Mark read so an unauthorized LIST: email isn't reprocessed every poll.
                imap.store(num, "+FLAGS", "\\Seen")
                continue

            url = _first_ali_url(subject, _text_body(msg))
            if not url:
                outcome["skipped"].append({"from": sender, "subject": subject, "reason": "no AliExpress URL"})
                imap.store(num, "+FLAGS", "\\Seen")
                continue

            # Mark read BEFORE listing so a crash mid-publish can't cause a double-list
            # on the next poll (the \Seen flag + dedup history are the idempotency guard).
            imap.store(num, "+FLAGS", "\\Seen")
            result = list_from_url.list_one(url, live=live)
            if live and not os.environ.get("INBOX_NO_EMAIL"):
                try:
                    import notify

                    notify.send(result)
                except Exception as exc:  # noqa: BLE001
                    result.setdefault("notes", []).append(f"reply email failed: {exc}")
            outcome["processed"].append({
                "from": sender, "url": url,
                "status": result.get("status"), "listed": result.get("listed_count", 0),
                "error": result.get("error", ""),
            })
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Actually publish each requested link. Without this, poll is a dry run.")
    args = parser.parse_args()
    try:
        outcome = poll(live=args.live)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(outcome, indent=2))
    print("MODE:", "LIVE — listings published" if args.live else "DRY RUN — nothing was listed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
