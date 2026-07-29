#!/usr/bin/env python3
"""Offline tests for the AliExpress product-by-id probe.

Never touches the network: ali_api._call and get_product_detail are monkeypatched. The
most important assertion here is the mapping one — that an affiliate-shaped response
flows through the parsers ali_api already has (_feed_products -> flatten_card) — because
that is what makes a token-free fallback cheap rather than a rewrite."""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
from typing import Any

import ali_api
import probe_ali_detail

PRODUCT_ID = "1005006000000001"

# The documented aliexpress.affiliate.productdetail.get envelope. Field names are the
# affiliate vocabulary; the point of the mapping test is that flatten_card already
# speaks it.
AFFILIATE_RESPONSE: dict[str, Any] = {
    "aliexpress_affiliate_productdetail_get_response": {
        "resp_result": {
            "resp_code": 200,
            "result": {
                "current_record_count": 1,
                "products": {
                    "product": [
                        {
                            "product_id": PRODUCT_ID,
                            "product_title": "Stainless Steel Pour Over Coffee Dripper",
                            "target_sale_price": "18.40",
                            "evaluate_rate": "95.4%",
                            "lastest_volume": "1200",
                            "product_main_image_url": "https://img/main.jpg",
                            "product_small_image_urls": {
                                "string": ["https://img/a.jpg", "https://img/b.jpg"]
                            },
                            "first_level_category_name": "Home & Garden",
                            "product_detail_url": f"https://www.aliexpress.com/item/{PRODUCT_ID}.html",
                        }
                    ]
                },
            },
        }
    }
}


@contextlib.contextmanager
def _patched(call, *, token: str = "", detail=None):
    original_call = ali_api._call
    original_detail = ali_api.get_product_detail
    original_env = {
        k: os.environ.get(k) for k in
        ("ALIEXPRESS_ACCESS_TOKEN", "ALIEXPRESS_APP_KEY", "ALIEXPRESS_APP_SECRET",
         "ALIEXPRESS_TRACKING_ID", "ALI_API_FIXTURE")
    }
    ali_api._call = call
    if detail is not None:
        ali_api.get_product_detail = detail
    os.environ.pop("ALI_API_FIXTURE", None)
    os.environ["ALIEXPRESS_ACCESS_TOKEN"] = token
    try:
        yield
    finally:
        ali_api._call = original_call
        ali_api.get_product_detail = original_detail
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run(call, *, token: str = "", detail=None) -> tuple[int, str]:
    buffer = io.StringIO()
    with _patched(call, token=token, detail=detail):
        with contextlib.redirect_stdout(buffer):
            code = probe_ali_detail.main_with_args([PRODUCT_ID])
    return code, buffer.getvalue()


# --- the mapping claim this whole probe rests on -----------------------------------

def test_affiliate_shape_is_extracted_by_existing_parsers() -> None:
    products = ali_api._feed_products(AFFILIATE_RESPONSE)
    assert len(products) == 1, "the affiliate envelope must match _feed_products"
    flat = ali_api.flatten_card(products[0])
    assert flat["id"] == PRODUCT_ID
    assert flat["title"].startswith("Stainless Steel")
    assert str(flat["price"]) == "18.40"
    assert flat["rating"] == 95.4 / 20.0, "evaluate_rate percent must become a 5-star rating"
    assert flat["orders"] == 1200
    assert flat["reviews"] is None, "affiliate carries no review count"
    assert flat["images"] == ["https://img/main.jpg", "https://img/a.jpg", "https://img/b.jpg"]
    assert flat["category"] == "Home & Garden"


def test_evaluation_accepts_a_complete_affiliate_response() -> None:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        evaluation = probe_ali_detail.evaluate_affiliate_payload(AFFILIATE_RESPONSE, PRODUCT_ID)
    assert evaluation["usable"] is True, evaluation
    assert not evaluation["missing"]


def test_viable_verdict_and_zero_exit() -> None:
    code, out = _run(lambda method, params: AFFILIATE_RESPONSE)
    assert probe_ali_detail.VIABLE in out
    assert code == 0


# --- the failure modes -------------------------------------------------------------

def test_permission_error_yields_the_no_permission_verdict() -> None:
    def denied(method: str, params: dict) -> dict:
        raise ali_api.AliError(
            f"{method} error 15: app has no permission IsvNoPermission "
            "The app does not have permission to call this api"
        )

    code, out = _run(denied)
    assert probe_ali_detail.NO_PERMISSION in out
    assert "IsvNoPermission" in out, "AliExpress's real reason must reach the report"
    assert "mint_ali_token.py" in out, "the report must name the remaining path"
    assert code == 1


def test_empty_response_yields_the_unusable_verdict() -> None:
    empty = {"aliexpress_affiliate_productdetail_get_response": {
        "resp_result": {"resp_code": 200, "result": {"products": {"product": []}}}}}
    code, out = _run(lambda method, params: empty)
    assert probe_ali_detail.UNUSABLE in out
    assert code == 1


def test_response_missing_a_price_is_unusable_and_says_so() -> None:
    import copy

    broken = copy.deepcopy(AFFILIATE_RESPONSE)
    product = broken["aliexpress_affiliate_productdetail_get_response"]["resp_result"]["result"]["products"]["product"][0]
    del product["target_sale_price"]
    code, out = _run(lambda method, params: broken)
    assert probe_ali_detail.UNUSABLE in out
    assert "price" in out
    assert code == 1


def test_one_failing_stage_does_not_abort_the_other() -> None:
    """A dead token must not stop the affiliate probe — the diagnosis must be complete."""
    def dead_token(product_id: str) -> dict:
        raise ali_api.AliError("ds.product.get error 27: Invalid access token")

    code, out = _run(lambda method, params: AFFILIATE_RESPONSE,
                     token="expired-token-value", detail=dead_token)
    assert "Invalid access token" in out, "the token failure must be reported"
    assert probe_ali_detail.VIABLE in out, "and the affiliate probe must still have run"
    assert code == 0


def test_token_probe_is_skipped_without_a_token() -> None:
    code, out = _run(lambda method, params: AFFILIATE_RESPONSE)
    assert "skipped" in out
    assert "ALIEXPRESS_ACCESS_TOKEN set : False" in out


# --- safety ------------------------------------------------------------------------

def test_no_credential_ever_reaches_stdout() -> None:
    secrets = {
        "ALIEXPRESS_APP_KEY": "APPKEY-must-not-print-123",
        "ALIEXPRESS_APP_SECRET": "APPSECRET-must-not-print-456",
        "ALIEXPRESS_TRACKING_ID": "TRACKING-must-not-print-789",
    }
    original = {k: os.environ.get(k) for k in secrets}
    os.environ.update(secrets)
    try:
        _, out = _run(lambda method, params: AFFILIATE_RESPONSE, token="TOKEN-must-not-print-000")
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    for value in list(secrets.values()) + ["TOKEN-must-not-print-000"]:
        assert value not in out, f"credential leaked into the report: {value}"
    # The booleans that replace them must still be there.
    assert "ALIEXPRESS_APP_KEY set      : True" in out


def test_probe_touches_nothing_on_ebay() -> None:
    source = Path(__file__).with_name("probe_ali_detail.py").read_text(encoding="utf-8")
    for forbidden in ("ebay_listing", "ebay_common", "EbayClient", "list_resilient"):
        assert forbidden not in source, f"the probe must not reach eBay ({forbidden})"


def test_probe_only_calls_read_methods() -> None:
    """Every AliExpress method the probe names must be a read. A mutating method
    appearing here would mean the diagnostic had grown teeth it must not have."""
    import re

    source = Path(__file__).with_name("probe_ali_detail.py").read_text(encoding="utf-8")
    # Bare method names only — the section headers carry trailing prose, so exclude
    # anything with a space in it.
    named = set(re.findall(r'"(aliexpress\.[a-z.]+)"', source))
    headers = set(re.findall(r'"(aliexpress\.[a-z.]+) ', source))
    assert named == {"aliexpress.affiliate.productdetail.get"}, named
    for method in named | headers:
        assert method.endswith(".get"), f"not a read method: {method}"


def test_workflow_is_read_only_and_passes_input_via_env() -> None:
    workflow = Path(__file__).parents[2] / ".github" / "workflows" / "probe-ali-detail.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "contents: read" in text, "the probe must not be able to write to the repo"
    assert "git push" not in text and "upload-artifact" not in text
    assert "PRODUCT: ${{ inputs.product }}" in text
    assert '"$PRODUCT"' in text
    for secret in ("EBAY_", "SMTP_", "OPENAI_", "GOOGLE_SERVICE_ACCOUNT"):
        assert secret not in text, f"the probe job must not receive {secret} secrets"


def _run_all() -> int:
    tests = [v for n, v in sorted(globals().items()) if n.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
