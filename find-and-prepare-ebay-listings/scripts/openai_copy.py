#!/usr/bin/env python3
"""Generate eBay listing copy (title + HTML description + item specifics) with OpenAI,
using the same prompt the Chrome extension ships. Vision-grounded on the product images.

Environment: OPENAI_API_KEY (required), OPENAI_MODEL (default gpt-4.1-mini).
Any failure raises CopyError so callers fall back to the deterministic template.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

# Ported verbatim from `ebay listing Chrome extension claude/openai.js`
# (exactEbayPromptTemplate) — the user's own eBay copywriter prompt.
EXACT_EBAY_PROMPT = (
    'Role: You are an Expert E-commerce Copywriter and SEO Strategist specialising in the eBay '
    'Global Marketplace. Your goal is to generate a listing that dominates search results in '
    'Australia, New Zealand, the USA, Canada, and the UK. Task: Using the attached supplier '
    'images and text, engineer a high-converting eBay Title and Technical Description. Generate a '
    'product title, ensuring the following rules are followed: Title length is 80 characters, max. '
    'Utilise as many of the 80 characters as possible without adding any fluff or dead words The '
    'Keyword Heavyweight: Start with the most descriptive, high-volume identity keywords. Zero '
    '"Dead" Words: Do not use "New," "Great," "Amazing," or "L@@K." The USP (Unique Selling Point): '
    'Include a key technical feature (e.g., "4x4 Rock Crawler" or "6-Note Scale"). Localisation: '
    'Include both Metric and Imperial measurements (e.g., 3" / 8cm) within the title for instant '
    'scale recognition. Generate a product description, ensuring the following rules are followed: '
    'The "Mobile-First" Hook: The first 200 characters must be a high-impact summary of the '
    "product's primary benefit. No \"Welcome to my store\" fluff. The \"Why\" Statement: A 2-3 "
    'sentence intro explaining how this product solves a problem or fulfils a need for the '
    'customer. Technical Features (3-5 Bullets): Focus on durability, functionality, and ease of '
    'use. Translate technical specs into direct user benefits (e.g., "Alloy Drive Shaft for '
    'high-torque durability on rough terrain"). Localisation: Use dual terminology (e.g., '
    '"Bonnet/Hood," "Spanner/Wrench") and dual measurements naturally. The "No-Surprise" '
    'Specification Suite Accurate Breakdown: Provide a clean, bolded list of exactly what is '
    'included in the box. Dimensions & Weight: Mandatory dual units for all 5 regions (cm/inch, '
    'lbs/oz, grams/kg). Material & Build: Clearly state materials (e.g., "ABS Plastic," "Carbon '
    'Steel") to build buyer trust. Formatting & Constraints: Tone: Professional, authoritative, '
    "and helpful. Keep the tone professional but helpful, don't over-embellish. Readability: Use "
    'bold headers, horizontal rules (---), and bullet points. Avoid walls of text. Prohibited: No '
    'mention of shipping, returns, or external links. Avoid "Dead Words." Visual Grounding: '
    'Cross-reference the attached images to ensure the text accurately reflects the colour, model, '
    'and accessories shown. [I HAVE ATTACHED THE IMAGES OF THE PRODUCT FOR REFERENCE. ANY TEXT '
    'BELOW THIS LINE IS USED AS REFERENCE MATERIAL TO MAKE AN ACCURATE LISTING DESCRIPTION AND '
    'TITLE]'
)

# Item-specifics guidance, adapted from the extension's buildEbayPrompt.
ITEM_SPECIFICS_RULES = (
    "\n\nItem specifics rules:\n"
    "- Use eBay's own field names as keys: Type, Material, Color, Model, Compatible Brand, "
    "Features, Number of Items, MPN, Custom Bundle, Modified Item.\n"
    "- Include every key you can support from the source data; add category-appropriate keys "
    "where the source supports them.\n"
    "- Do NOT include Country/Region of Manufacture or Country/Region of Origin.\n"
    "- Never state a country of origin/manufacture anywhere, including in the description "
    "text or its specification list. Omit it entirely.\n"
    "- Keep each value under 65 characters, one plain value per key (no lists).\n"
    '- Brand must be "Unbranded"; MPN must be "N/A".\n'
)

SYSTEM = "You generate accurate, policy-conscious ecommerce listing data as strict JSON."

# eBay renders the description field as HTML, so Markdown shows up as literal "**" and
# "---" characters. Demand real tags.
HTML_RULES = (
    "\n\nDESCRIPTION FORMAT — CRITICAL:\n"
    "The description field is rendered as raw HTML on eBay. Output HTML tags only.\n"
    "- Never use Markdown. Do not output **bold**, *italic*, ---, or '- ' bullets.\n"
    "- Bold/headers: <h3>Heading</h3> and <strong>text</strong>\n"
    "- Bullets: <ul><li>point</li><li>point</li></ul>\n"
    "- Horizontal rule: <hr>\n"
    "- Paragraphs: <p>text</p>\n"
    "- Line breaks: <br>\n"
    "Example: <p>Intro sentence.</p><h3>Key Features</h3><ul><li><strong>Durable:</strong> "
    "ABS build.</li></ul><hr><h3>Specifications</h3><ul><li>Length: 67cm / 26.4in</li></ul>\n"
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "description", "itemSpecifics"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "itemSpecifics": {"type": "object", "additionalProperties": {"type": "string"}},
    },
}


class CopyError(RuntimeError):
    pass


# What makes a product worth listing. Illustrative categories the user has had success
# with, plus the general appeal traits — not an exhaustive list.
RANKING_PROMPT = (
    "You are a seasoned eBay reseller choosing which AliExpress products to list.\n"
    "Score each candidate 0-10 for resale appeal to a US eBay buyer.\n\n"
    "Score HIGH for products that are:\n"
    "- gaming gear and accessories; superhero / comic / pop-culture themed items\n"
    "- RC vehicles (cars, helicopters, planes), drones\n"
    "- giftable impulse buys: novel, fun, cute or clever, instantly appealing in a photo\n"
    "- practical problem-solvers: clever gadgets that fix an everyday annoyance\n"
    "- trending or viral consumer products with social buzz\n"
    "- home & lifestyle upgrades: decor, lighting, organisation that photographs well\n"
    "(These are examples of what has worked, not an exhaustive list — reward anything a\n"
    "browsing buyer would find genuinely cool, useful or giftable.)\n\n"
    "Score 0-2 and never recommend:\n"
    "- spare parts, replacement components, repair items (screws, fittings, connectors,\n"
    "  cables, PCBs, keycaps, LCD/digitizer replacements, tubing, bearings)\n"
    "- generic commodity hardware or anything only a technician would buy\n"
    "- items whose purpose is unclear from the title\n\n"
    "Return the best candidates in descending score order. Exclude anything scoring below 5."
)

RANKING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ranked"],
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "score", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "number"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}


_BLOCK_TAG = re.compile(r"<(p|ul|ol|li|h[1-6]|div|br|hr|table)\b", re.I)


def to_html(text: str) -> str:
    """Safety net: convert any Markdown the model emits into eBay-safe HTML.

    eBay renders the description as HTML, so '**bold**', '---' and '- ' bullets would
    otherwise appear as literal characters. Already-HTML input is passed through with
    only stray Markdown cleaned up.
    """
    text = (text or "").strip()
    if not text:
        return text
    # Inline markdown is safe to convert in both branches.
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text, flags=re.S)

    if _BLOCK_TAG.search(text):  # model complied — just normalise leftover rules
        text = re.sub(r"(?m)^\s*-{3,}\s*$", "<hr>", text)
        return text.replace(" --- ", "<hr>")

    # Markdown fallback: treat '---' as a block separator, then build blocks.
    text = re.sub(r"\s*-{3,}\s*", "\n---\n", text)
    blocks: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "---":
            flush()
            blocks.append("<hr>")
            continue
        bullet = re.match(r"^[-*•]\s+(.*)$", line)
        if bullet:
            bullets.append(bullet.group(1).strip())
            continue
        # A single run-on line can hold several " - " bullets; split when there are 2+.
        parts = re.split(r"\s+-\s+(?=\S)", line)
        if len(parts) > 2:
            flush()
            head = parts[0].strip()
            if head:
                blocks.append(f"<p>{head}</p>")
            blocks.append("<ul>" + "".join(f"<li>{p.strip()}</li>" for p in parts[1:] if p.strip()) + "</ul>")
            continue
        flush()
        blocks.append(f"<p>{line}</p>")
    flush()
    return "".join(blocks)


def _usage(payload: dict[str, Any]) -> tuple[int, int]:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)


def _post(body: dict[str, Any], transport: Any | None) -> dict[str, Any]:
    if transport is not None:
        return transport(body)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CopyError(f"OpenAI request failed: {exc}") from exc


def rank_candidates(
    candidates: list[dict[str, Any]],
    top_n: int = 6,
    transport: Any | None = None,
) -> dict[str, Any]:
    """Score candidates for resale appeal.

    ``candidates`` are dicts with 'id', 'title' and optional 'price'. Returns
    {'ranked': [{id, score, reason}], 'usage': (in, out), 'model': str} ordered best-first.
    Raises CopyError so callers can fall back to deterministic order.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key and transport is None:
        raise CopyError("OPENAI_API_KEY is not set")
    if not candidates:
        raise CopyError("no candidates to rank")
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"

    listing = [
        {"id": str(item.get("id", "")), "title": str(item.get("title", ""))[:200],
         "price_usd": str(item.get("price", ""))}
        for item in candidates
    ]
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": "You rank ecommerce products and reply with strict JSON."},
            {"role": "user", "content": [{
                "type": "input_text",
                "text": RANKING_PROMPT
                + f"\n\nReturn at most {top_n} candidates.\n\nCandidates:\n"
                + json.dumps(listing, ensure_ascii=False),
            }]},
        ],
        "text": {"format": {"type": "json_schema", "name": "ranked_products", "strict": False, "schema": RANKING_SCHEMA}},
    }
    payload = _post(body, transport)
    try:
        parsed = json.loads(_extract_text(payload))
    except (json.JSONDecodeError, TypeError) as exc:
        raise CopyError(f"OpenAI returned unparseable ranking: {exc}") from exc
    ranked = [
        {"id": str(r.get("id", "")), "score": float(r.get("score", 0) or 0), "reason": str(r.get("reason", ""))}
        for r in (parsed.get("ranked") or [])
        if isinstance(r, dict) and str(r.get("id", ""))
    ]
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return {"ranked": ranked[:top_n], "usage": _usage(payload), "model": model}


def _extract_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"]
    for item in payload.get("output", []) if isinstance(payload.get("output"), list) else []:
        for node in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(node, dict) and node.get("text"):
                return str(node["text"])
    raise CopyError("OpenAI response did not include listing JSON")


def generate_listing(
    source_title: str,
    category: str,
    price: str,
    image_urls: list[str],
    transport: Any | None = None,
) -> dict[str, Any]:
    """Return {'title', 'description', 'item_specifics'}. Raises CopyError on failure."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise CopyError("OPENAI_API_KEY is not set")
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"

    product = {"title": source_title, "category": category, "price_usd": price}
    prompt = (
        EXACT_EBAY_PROMPT
        + ITEM_SPECIFICS_RULES
        + HTML_RULES
        + "\n\nSource product data:\n"
        + json.dumps(product, ensure_ascii=False)
    )
    user_content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for url in image_urls[:8]:
        if isinstance(url, str) and url.startswith("https://"):
            user_content.append({"type": "input_image", "image_url": url, "detail": "low"})

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "text": {"format": {"type": "json_schema", "name": "ebay_listing", "strict": False, "schema": SCHEMA}},
    }

    payload = _post(body, transport)
    try:
        listing = json.loads(_extract_text(payload))
    except (json.JSONDecodeError, TypeError) as exc:
        raise CopyError(f"OpenAI returned unparseable listing: {exc}") from exc

    title = str(listing.get("title", "")).strip()
    # eBay renders this as HTML — convert any Markdown the model still emitted.
    description = to_html(str(listing.get("description", "")).strip())
    if not title or not description:
        raise CopyError("OpenAI listing missing title or description")
    specifics_raw = listing.get("itemSpecifics", {})
    item_specifics = {
        str(k).strip(): str(v).strip()
        for k, v in (specifics_raw.items() if isinstance(specifics_raw, dict) else [])
        if str(k).strip() and str(v).strip()
    }
    return {
        "title": title,
        "description": description,
        "item_specifics": item_specifics,
        "usage": _usage(payload),
        "model": model,
    }
