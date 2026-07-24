#!/usr/bin/env python3
"""Generate eBay listing copy (title + HTML description + item specifics) with OpenAI,
using the same prompt the Chrome extension ships. Vision-grounded on the product images.

Environment: OPENAI_API_KEY (required), OPENAI_MODEL (default gpt-4.1-mini).
Any failure raises CopyError so callers fall back to the deterministic template.
"""

from __future__ import annotations

import json
import os
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
    "- Keep each value under 65 characters, one plain value per key (no lists).\n"
    '- Brand must be "Unbranded"; MPN must be "N/A".\n'
)

SYSTEM = "You generate accurate, policy-conscious ecommerce listing data as strict JSON."

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

    if transport is not None:
        payload = transport(body)
    else:
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise CopyError(f"OpenAI request failed: {exc}") from exc

    try:
        listing = json.loads(_extract_text(payload))
    except (json.JSONDecodeError, TypeError) as exc:
        raise CopyError(f"OpenAI returned unparseable listing: {exc}") from exc

    title = str(listing.get("title", "")).strip()
    description = str(listing.get("description", "")).strip()
    if not title or not description:
        raise CopyError("OpenAI listing missing title or description")
    specifics_raw = listing.get("itemSpecifics", {})
    item_specifics = {
        str(k).strip(): str(v).strip()
        for k, v in (specifics_raw.items() if isinstance(specifics_raw, dict) else [])
        if str(k).strip() and str(v).strip()
    }
    return {"title": title, "description": description, "item_specifics": item_specifics}
