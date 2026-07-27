#!/usr/bin/env python3
"""Render drafts as a visual HTML review page.

The Drafts sheet is where you *edit*; this is where you *look*. It shows each draft the
way the listing will read — image gallery, title, formatted description, item specifics,
variants and prices — so verifying takes a glance instead of scrolling a spreadsheet.

The same markup is used two ways: written to a file and uploaded as a workflow artifact,
and embedded in the "drafts ready" email so it renders on a phone. It is therefore fully
self-contained (inline CSS, no scripts, no external assets beyond the product images
themselves, which are hosted on the AliExpress CDN).
"""

from __future__ import annotations

import argparse
import html
import re
from typing import Any

# The description is model-written HTML (see openai_copy.HTML_RULES). Render it, but only
# from a small safe subset — this markup is emailed and opened in a browser, so no
# scripts, styles, iframes, or event handlers get through.
_ALLOWED_TAGS = {"p", "br", "ul", "ol", "li", "b", "strong", "i", "em", "h3", "h4", "span", "div"}
_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9]+)[^>]*>")


def sanitize_description(markup: str) -> str:
    def replace(match: re.Match[str]) -> str:
        closing, tag = match.group(1), match.group(2).lower()
        if tag not in _ALLOWED_TAGS:
            return ""
        return f"<{closing}{tag}>"

    return _TAG_RE.sub(replace, str(markup or ""))


STYLE = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
line-height:1.5;color:#1a1a1a;background:#f6f7f9;margin:0;padding:24px}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:#666;font-size:14px;margin:0 0 24px}
.card{background:#fff;border:1px solid #e2e5e9;border-radius:10px;padding:20px;margin-bottom:24px}
.card h2{font-size:18px;margin:0 0 6px}
.meta{color:#666;font-size:13px;margin:0 0 14px}
.price{font-size:24px;font-weight:700;color:#0b7a3b;margin:0 0 2px}
.cost{color:#666;font-size:13px;margin:0 0 16px}
.gallery{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.gallery img{width:104px;height:104px;object-fit:cover;border:1px solid #e2e5e9;border-radius:6px}
table{border-collapse:collapse;width:100%;font-size:14px;margin-bottom:16px}
th,td{border:1px solid #e2e5e9;padding:7px 10px;text-align:left;vertical-align:top}
th{background:#f2f4f6;font-weight:600;width:220px}
.desc{border:1px solid #e2e5e9;border-radius:6px;padding:14px;background:#fbfcfd;font-size:14px}
.warn{background:#fff6e5;border:1px solid #f0c36d;border-radius:6px;padding:10px 14px;
font-size:13px;margin-bottom:16px}
.warn b{color:#8a5a00}
.spare{color:#666;font-size:12px;word-break:break-all;margin-top:8px}
.tag{display:inline-block;background:#eef1f4;border-radius:4px;padding:2px 8px;font-size:12px;
color:#444;margin-right:6px}
a{color:#0b5fa5}
"""


def _gallery(images: list[str], limit: int = 12) -> str:
    cells = "".join(
        f'<img src="{html.escape(url)}" alt="">' for url in (images or [])[:limit]
    )
    return f'<div class="gallery">{cells}</div>' if cells else ""


def _specifics_table(aspects: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><th>{html.escape(str(name))}</th><td>"
        f"{html.escape(', '.join(str(v) for v in (values if isinstance(values, list) else [values])))}"
        "</td></tr>"
        for name, values in (aspects or {}).items()
    )
    return f"<table>{rows}</table>" if rows else "<p><i>No item specifics.</i></p>"


def _variants_table(variants: list[dict[str, Any]]) -> str:
    if not variants:
        return ""
    head = (
        "<tr><th style='width:auto'>Variation</th><th style='width:auto'>Options</th>"
        "<th style='width:auto'>Cost</th><th style='width:auto'>eBay price</th></tr>"
    )
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(variant.get('id', '')))}</td>"
        f"<td>{html.escape(', '.join(f'{k}: {v}' for k, v in (variant.get('options') or {}).items())) or '—'}</td>"
        f"<td>USD {html.escape(str(variant.get('delivered_total', '')))}</td>"
        f"<td>USD {html.escape(str(variant.get('expected_ebay_price', '')))}</td>"
        "</tr>"
        for variant in variants
    )
    return f"<table>{head}{rows}</table>"


def draft_card(draft: dict[str, Any]) -> str:
    source = draft.get("source", {}) or {}
    variants = source.get("selected_variants") or []
    first = variants[0] if variants else {}
    validation = draft.get("validation") or {}
    warnings = list(validation.get("warnings") or []) + list(draft.get("notes") or [])
    spare = draft.get("spare_images") or []

    warn_html = ""
    if warnings:
        items = "".join(f"<li>{html.escape(str(item))}</li>" for item in warnings)
        warn_html = f'<div class="warn"><b>Needs a look</b><ul>{items}</ul></div>'

    spare_html = ""
    if spare:
        links = "<br>".join(
            f'<a href="{html.escape(url)}">{html.escape(url)}</a>' for url in spare[:12]
        )
        spare_html = (
            f'<p class="spare"><b>{len(spare)} spare image(s)</b> found on the AliExpress '
            f"page but not in this listing — copy any into the sheet's Images cell:<br>{links}</p>"
        )

    return f"""
<div class="card">
  <h2>{html.escape(str(source.get('listing_title', '(untitled)')))}</h2>
  <p class="meta">
    <span class="tag">{html.escape(str(draft.get('status', '')))}</span>
    <span class="tag">{html.escape(str(source.get('assigned_niche', '')))}</span>
    Draft <code>{html.escape(str(draft.get('draft_id', '')))}</code> ·
    <a href="{html.escape(str(source.get('aliexpress_url', '')))}">AliExpress source</a>
  </p>
  {warn_html}
  <p class="price">USD {html.escape(str(first.get('expected_ebay_price', '')))}</p>
  <p class="cost">Delivered cost USD {html.escape(str(first.get('delivered_total', '')))}
     · category {html.escape(str(validation.get('category_id', '')) or 'unresolved')}
     · {len(source.get('source_images') or [])} image(s)</p>
  {_gallery(source.get('source_images') or [])}
  {_variants_table(variants)}
  <h3>Item specifics</h3>
  {_specifics_table(source.get('aspects') or {})}
  <h3>Description</h3>
  <div class="desc">{sanitize_description(source.get('listing_description', ''))}</div>
  {spare_html}
</div>
"""


def render(drafts: list[dict[str, Any]], *, sheet_url: str = "", date: str = "") -> str:
    cards = "".join(draft_card(draft) for draft in drafts)
    sheet_link = (
        f'<p class="sub">Edit and approve in the '
        f'<a href="{html.escape(sheet_url)}">Drafts tab</a>, then run the '
        "<b>Publish drafts</b> workflow.</p>"
        if sheet_url else ""
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>eBay listing drafts{' — ' + html.escape(date) if date else ''}</title>
<style>{STYLE}</style></head>
<body><div class="wrap">
<h1>📝 {len(drafts)} eBay listing draft(s) ready for review</h1>
<p class="sub">Nothing is live. Nothing has been created on eBay.{
    ' — ' + html.escape(date) if date else ''}</p>
{sheet_link}
{cards}
</div></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Path to write the HTML page to")
    parser.add_argument("--sheet-url", default="")
    args = parser.parse_args()
    import draft_store
    from pathlib import Path

    drafts = [item for item in draft_store.load_all() if draft_store.is_publishable(item)]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(drafts, sheet_url=args.sheet_url), encoding="utf-8")
    print(f"Wrote {len(drafts)} draft(s) to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
