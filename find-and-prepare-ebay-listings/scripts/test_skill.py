#!/usr/bin/env python3
"""Offline regression tests for the API-first eBay listing skill."""

from __future__ import annotations

import json
import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from candidate_ledger import append_record, summarize
from daily_history import load_history
from ebay_common import ApiError, EbayClient, EbayError, Keychain, Response, SCOPES, UnknownOutcome, safe_url
from ebay_listing import category_and_aspects, prepare, publish, rollback
from ebay_price import estimated_margin, quote, round_up_destination_price
from ebay_setup import fulfillment_is_free, policy_allows_standard, seller_ready, standard_ads_eligible
from extension_job import extension_state_action
from listing_job import (
    JobError,
    build_review,
    deterministic_group_key,
    deterministic_sku,
    initialize_result,
    normalize_source,
    read_json,
    record_prepared,
)
from run_budget import BudgetError, consume, initialize
from variant_rank import select_variants


def source(product_id: str = "1005000000000001", run: str = "run-a") -> dict:
    return {
        "run_id": f"{run}-product-{product_id}",
        "local_calendar_date": "2026-07-21",
        "assigned_niche": "Smartphone Accessories",
        "product_id": product_id,
        "aliexpress_url": f"https://www.aliexpress.us/item/{product_id}.html",
        "source_title": f"Source {product_id}",
        "functional_fingerprint": f"function {product_id}",
        "verified_brand": "Unbranded",
        "listing_title": "USB C Car Charger Adapter Fast Charging",
        "listing_description": "Factual verified product description.",
        "condition": "NEW",
        "category_query": "USB C car charger",
        "aspects": {"Brand": ["Unbranded"], "Type": ["Car Charger"]},
        "source_images": ["https://example.com/one.jpg"],
        "selected_variants": [{
            "id": "black", "options": {}, "visible_item_price": "17.25",
            "delivered_total": "18.40", "quantity": 1,
        }],
    }


def prepared_api(normalized: dict) -> dict:
    offers = [
        {"sku": item["sku"], "offer_id": f"offer-{index}", "published": False, "readback": {"sku": item["sku"]}}
        for index, item in enumerate(normalized["selected_variants"], 1)
    ]
    return {
        "environment": "production",
        "marketplace_id": "EBAY_US",
        "merchant_location_key": "irvine-92618",
        "payment_policy_id": "pay",
        "return_policy_id": "return",
        "fulfillment_policy_id": "ship",
        "campaign_id": "campaign",
        "promoted_rate_percent": "10.0",
        "category_id": "123",
        "required_aspects_complete": True,
        "normalized_aspects": normalized["aspects"],
        "eps_image_urls": ["https://i.ebayimg.com/images/g/test/s-l1600.jpg"],
        "image_import_failures": [],
        "inventory_items": [{"sku": item["sku"], "readback": {}} for item in normalized["selected_variants"]],
        "inventory_item_group": None,
        "offers": offers,
        "listing_fees": {"feeSummaries": []},
    }


class WorkflowContractTests(unittest.TestCase):
    def test_skill_is_api_first_and_backup_is_explicit(self):
        root = Path(__file__).resolve().parent.parent
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        backup = (root / "references" / "extension-backup.md").read_text(encoding="utf-8")
        self.assertIn("Use the official eBay APIs by default", skill)
        self.assertIn("Never fall back", skill)
        self.assertIn("explicitly requests", backup)
        self.assertIn("ebay listing Chrome extension claude", backup)

    def test_publish_is_a_separate_exact_run_id_action(self):
        skill = (Path(__file__).resolve().parent.parent / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("later turn", skill)
        self.assertIn("--confirm-run-id <exact-run-id>", skill)
        self.assertIn("withdraw the whole pair", skill)

    def test_secrets_and_address_are_excluded_from_artifacts(self):
        root = Path(__file__).resolve().parent.parent
        schema = (root / "references" / "handoff-schema.md").read_text(encoding="utf-8")
        self.assertIn("Never store credentials", schema)
        for path in root.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                self.assertNotIn("171 " + "Bright Poppy", path.read_text(encoding="utf-8", errors="ignore"), str(path))

    def test_prepare_code_has_no_publish_operation(self):
        self.assertNotIn("publish_product", inspect.getsource(prepare))
        listing = (Path(__file__).resolve().parent / "ebay_listing.py").read_text(encoding="utf-8")
        self.assertIn('https://apim.ebay.com/commerce/media/v1_beta/image/create_image_from_url', listing)
        self.assertNotIn('/sell/media/v1_beta', listing)


class KeychainTests(unittest.TestCase):
    def test_safe_url_redacts_authorization_code(self):
        safe = safe_url("https://example.com/accepted?code=secret&state=ok")
        self.assertNotIn("secret", safe)
        self.assertIn("state=ok", safe)

    def test_keychain_writes_secret_via_stdin_not_process_arguments(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "", "")

        Keychain(service="test", runner=runner).set("client_secret", "very-secret")
        args, kwargs = calls[0]
        self.assertNotIn("very-secret", args)
        self.assertEqual(kwargs["input"], "very-secret\n")
        self.assertEqual(args[-1], "-w")

    def test_oauth_refresh_uses_production_scopes_and_caches_access_token_in_memory(self):
        class Secrets:
            values = {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
            def get(self, key):
                return self.values[key]

        class Transport:
            def __init__(self):
                self.calls = []
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return Response(200, {}, {"access_token": "memory-only", "expires_in": 7200}, url)

        transport = Transport()
        client = EbayClient(keychain=Secrets(), transport=transport)
        self.assertEqual(client.access_token(), "memory-only")
        self.assertEqual(client.access_token(), "memory-only")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][1], "https://api.ebay.com/identity/v1/oauth2/token")
        self.assertEqual(set(transport.calls[0][2]["form_body"]["scope"].split()), set(SCOPES))

    def test_revoked_refresh_token_is_reported_without_retry(self):
        class Secrets:
            def get(self, key):
                return {"client_id": "id", "client_secret": "secret", "refresh_token": "revoked"}[key]
        class Transport:
            def request(self, method, url, **kwargs):
                return Response(400, {}, {"error": "invalid_grant"}, url)
        with self.assertRaises(ApiError):
            EbayClient(keychain=Secrets(), transport=Transport()).access_token()


class SourceValidationTests(unittest.TestCase):
    def test_normalizes_source_and_uses_stable_sku(self):
        first = normalize_source(source())
        second = normalize_source(source())
        self.assertEqual(first["selected_variants"][0]["sku"], second["selected_variants"][0]["sku"])
        self.assertTrue(first["selected_variants"][0]["sku"].startswith("ALI-1005000000000001-"))
        self.assertEqual(first["selected_variants"][0]["expected_ebay_price"], "70.00")

    def test_rejects_missing_verified_facts(self):
        payload = source()
        payload["aspects"] = {}
        with self.assertRaisesRegex(JobError, "aspects"):
            normalize_source(payload)
        payload = source()
        payload["source_images"] = []
        with self.assertRaisesRegex(JobError, "source_images"):
            normalize_source(payload)

    def test_multi_variation_requires_matching_axes_and_group(self):
        payload = source()
        payload["selected_variants"] = [
            {"id": "black", "options": {"Color": "Black"}, "visible_item_price": "17.25", "delivered_total": "18.40"},
            {"id": "white", "options": {"Color": "White"}, "visible_item_price": "17.25", "delivered_total": "18.40"},
        ]
        normalized = normalize_source(payload)
        self.assertEqual(normalized["inventory_item_group_key"], deterministic_group_key(payload["product_id"]))
        self.assertEqual(len({item["sku"] for item in normalized["selected_variants"]}), 2)

        payload["selected_variants"][1]["options"]["Color"] = "Black"
        with self.assertRaisesRegex(JobError, "two distinct"):
            normalize_source(payload)

        payload = source()
        payload["source_images"] = [f"https://example.com/{index}.jpg" for index in range(13)]
        payload["selected_variants"] = [
            {"id": "black", "options": {"Color": "Black"}, "visible_item_price": "17.25", "delivered_total": "18.40"},
            {"id": "white", "options": {"Color": "White"}, "visible_item_price": "17.25", "delivered_total": "18.40"},
        ]
        with self.assertRaisesRegex(JobError, "at most 12"):
            normalize_source(payload)

    def test_visible_price_and_checkout_cost_contract(self):
        payload = source()
        payload["selected_variants"][0]["visible_item_price"] = "14.99"
        with self.assertRaisesRegex(JobError, "below USD 15"):
            normalize_source(payload)
        payload = source()
        payload["selected_variants"][0]["delivered_total"] = "0"
        with self.assertRaisesRegex(JobError, "must be positive"):
            normalize_source(payload)


class PreparedReviewTests(unittest.TestCase):
    def initialized(self, root: Path, product_id: str, run: str = "batch") -> Path:
        product = root / product_id
        product.mkdir(parents=True)
        source_path = product / "source.json"
        result_path = product / "result.json"
        source_path.write_text(json.dumps(source(product_id, run)), encoding="utf-8")
        initialize_result(source_path, result_path)
        return result_path

    def test_record_prepared_requires_unpublished_offers_and_fees(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.initialized(Path(directory), "1005000000000001")
            normalized = read_json(result)
            record = prepared_api(normalized)
            prepared = record_prepared(result, record)
            self.assertEqual(prepared["status"], "api_prepared")
            self.assertFalse(prepared["publish_allowed"])
            self.assertFalse(prepared["published"])
            result = self.initialized(Path(directory), "1005000000000002")
            record = prepared_api(read_json(result))
            record["offers"][0]["published"] = True
            with self.assertRaisesRegex(JobError, "unpublished offer ID"):
                record_prepared(result, record)

    def test_review_requires_exactly_two_distinct_prepared_products(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for product_id in ("1005000000000001", "1005000000000002"):
                path = self.initialized(root, product_id)
                record_prepared(path, prepared_api(read_json(path)))
                paths.append(path)
            review = build_review(paths, root / "run-result.json", root / "review.md")
            self.assertEqual(review["status"], "api_prepared")
            self.assertFalse(review["published"])
            text = (root / "review.md").read_text(encoding="utf-8")
            self.assertIn("Nothing is live", text)
            self.assertIn(review["run_id"], text)


class FakeTaxonomyClient:
    def request(self, method, path, **kwargs):
        if "get_default_category_tree_id" in path:
            return Response(200, {}, {"categoryTreeId": "0"}, path)
        if "get_category_suggestions" in path:
            return Response(200, {}, {"categorySuggestions": [{"category": {"categoryId": "123"}}]}, path)
        if "get_item_aspects_for_category" in path:
            return Response(200, {}, {"aspects": [
                {"localizedAspectName": "Brand", "aspectConstraint": {"aspectRequired": True}},
                {"localizedAspectName": "Type", "aspectConstraint": {"aspectRequired": True, "aspectMode": "SELECTION_ONLY"},
                 "aspectValues": [{"localizedValue": "Car Charger"}]},
            ]}, path)
        if "get_item_condition_policies" in path:
            return Response(200, {}, {"itemConditionPolicies": [{"itemConditions": [{"conditionEnum": "NEW"}]}]}, path)
        raise AssertionError(path)


class TaxonomyTests(unittest.TestCase):
    def test_category_required_aspects_and_condition_are_validated(self):
        normalized = normalize_source(source())
        category, aspects, missing = category_and_aspects(FakeTaxonomyClient(), normalized)
        self.assertEqual(category, "123")
        self.assertEqual(aspects["Type"], ["Car Charger"])
        self.assertEqual(missing, [])

    def test_missing_required_aspect_blocks(self):
        payload = source()
        payload["aspects"] = {"Brand": ["Unbranded"]}
        normalized = normalize_source(payload)
        with self.assertRaisesRegex(EbayError, "Missing required"):
            category_and_aspects(FakeTaxonomyClient(), normalized)


class PublishClient:
    def request(self, method, path, **kwargs):
        if method == "GET" and "/offer/" in path:
            offer_id = path.rstrip("/").rsplit("/", 1)[-1]
            suffix = offer_id.rsplit("-", 1)[-1]
            return Response(200, {}, {"sku": f"sku-{suffix}", "status": "UNPUBLISHED"}, path)
        return Response(204, {}, None, path)


class PublishContractTests(unittest.TestCase):
    def run_file(self, root: Path) -> dict:
        products = []
        for index, product_id in enumerate(("1005000000000001", "1005000000000002"), 1):
            item = normalize_source(source(product_id, "batch"))
            item.update({
                "status": "api_prepared", "published": False, "publish_allowed": False,
                "api": {"offers": [{
                    "sku": f"sku-{index}", "offer_id": f"offer-{index}", "published": False,
                    "readback": {"sku": f"sku-{index}", "status": "UNPUBLISHED"},
                }], "inventory_item_group": None},
            })
            products.append(item)
        run = {"status": "api_prepared", "run_id": "batch", "published": False, "publish_allowed": False, "products": products}
        write_json = __import__("ebay_common").write_json
        write_json(root / "run-result.json", run)
        return run

    def setup_config(self):
        return {"campaign_id": "campaign", "merchant_location_key": "irvine-92618", "promoted_rate_percent": "10.0"}

    def test_wrong_confirmation_cannot_publish(self):
        with tempfile.TemporaryDirectory() as directory, patch("ebay_listing.require_setup", return_value=self.setup_config()):
            root = Path(directory)
            self.run_file(root)
            with self.assertRaisesRegex(EbayError, "does not match"):
                publish(root, "wrong", PublishClient(), root / "history.jsonl")
            self.assertFalse(read_json(root / "run-result.json")["published"])

    def test_duplicate_or_changed_offer_blocks_before_publish(self):
        with tempfile.TemporaryDirectory() as directory, patch("ebay_listing.require_setup", return_value=self.setup_config()):
            root = Path(directory)
            run = self.run_file(root)
            run["products"][1]["api"]["offers"][0]["offer_id"] = "offer-1"
            __import__("ebay_common").write_json(root / "run-result.json", run)
            with self.assertRaisesRegex(EbayError, "duplicated offer IDs"):
                publish(root, "batch", PublishClient(), root / "history.jsonl")

    def test_success_marks_both_live_then_writes_history(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("ebay_listing.require_setup", return_value=self.setup_config()), \
             patch("ebay_listing.publish_product", side_effect=[
                 {"listing_id": "123456789001", "ebay_url": "https://www.ebay.com/itm/123456789001"},
                 {"listing_id": "123456789002", "ebay_url": "https://www.ebay.com/itm/123456789002"},
             ]), \
             patch("ebay_listing.published_listing_id", side_effect=["123456789001", "123456789002"]), \
             patch("ebay_listing.promote", side_effect=["ad-1", "ad-2"]):
            root = Path(directory)
            self.run_file(root)
            result = publish(root, "batch", PublishClient(), root / "history.jsonl")
            self.assertEqual(result["status"], "live")
            self.assertEqual(len(load_history(root / "history.jsonl")), 2)
            self.assertTrue(all(item["priority_promotion_enabled"] is False for item in result["products"]))

    def test_second_failure_rolls_back_whole_pair(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("ebay_listing.require_setup", return_value=self.setup_config()), \
             patch("ebay_listing.publish_product", side_effect=[
                 {"listing_id": "123456789001", "ebay_url": "https://www.ebay.com/itm/123456789001"},
                 EbayError("second failed"),
             ]), \
             patch("ebay_listing.published_listing_id", return_value="123456789001"), \
             patch("ebay_listing.promote", return_value="ad-1"), \
             patch("ebay_listing.rollback", return_value=[]) as compensate:
            root = Path(directory)
            self.run_file(root)
            with self.assertRaisesRegex(EbayError, "whole-pair rollback"):
                publish(root, "batch", PublishClient(), root / "history.jsonl")
            self.assertEqual(read_json(root / "run-result.json")["status"], "publish_rolled_back")
            compensate.assert_called_once()
            self.assertFalse((root / "history.jsonl").exists())

    def test_promotion_failure_rolls_back_the_just_published_listing(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("ebay_listing.require_setup", return_value=self.setup_config()), \
             patch("ebay_listing.publish_product", return_value={
                 "listing_id": "123456789001", "ebay_url": "https://www.ebay.com/itm/123456789001",
             }), \
             patch("ebay_listing.promote", side_effect=EbayError("promotion failed")), \
             patch("ebay_listing.rollback", return_value=[]) as compensate:
            root = Path(directory)
            self.run_file(root)
            with self.assertRaisesRegex(EbayError, "whole-pair rollback"):
                publish(root, "batch", PublishClient(), root / "history.jsonl")
            rolled = compensate.call_args.args[2]
            self.assertEqual(rolled[0]["listing_id"], "123456789001")
            self.assertEqual(read_json(root / "run-result.json")["status"], "publish_rolled_back")

    def test_ambiguous_mutation_requires_reconciliation_even_after_clean_compensation(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("ebay_listing.require_setup", return_value=self.setup_config()), \
             patch("ebay_listing.publish_product", side_effect=UnknownOutcome("timed out")), \
             patch("ebay_listing.rollback", return_value=[]):
            root = Path(directory)
            self.run_file(root)
            with self.assertRaises(EbayError):
                publish(root, "batch", PublishClient(), root / "history.jsonl")
            self.assertEqual(read_json(root / "run-result.json")["status"], "reconciliation_required")
            self.assertFalse((root / "history.jsonl").exists())

    def test_rollback_deletes_ads_before_withdrawing(self):
        calls = []

        class Client:
            def request(self, method, path, **kwargs):
                calls.append((method, path))
                return Response(204, {}, None, path)

        product = {"api": {"offers": [{"offer_id": "offer-1"}], "inventory_item_group": None}}
        errors = rollback(Client(), "campaign", [{"product": product, "ad_id": "ad-1"}])
        self.assertEqual(errors, [])
        self.assertEqual(calls[0][0], "DELETE")
        self.assertIn("withdraw", calls[1][1])


class RetainedHelperTests(unittest.TestCase):
    def test_extension_backup_state_machine_still_never_retries(self):
        decision = extension_state_action("error", clicked_once=True, observed_after_click=True)
        self.assertEqual(decision["action"], "stop")
        self.assertFalse(decision["retry_click"])

    def test_pricing_reaches_target_margin(self):
        result = quote(18.40)
        margin = estimated_margin(float(result["suggested_price"]), 18.40, 0.1325, 0.10, 0.30)
        self.assertGreaterEqual(margin, 0.50)
        self.assertEqual(round_up_destination_price(29.15), 30.0)

    def test_free_shipping_policy_detection(self):
        policy = {"shippingOptions": [{"optionType": "DOMESTIC", "shippingServices": [{"shippingCost": {"value": "0.00"}}]}]}
        self.assertTrue(fulfillment_is_free(policy))

    def test_seller_policy_and_general_ads_gates(self):
        policy = {"categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}]}
        self.assertTrue(policy_allows_standard(policy))
        account = {
            "privileges": {"sellerRegistrationCompleted": True, "sellingLimit": {"quantity": 10, "amount": {"value": "100.00"}}},
            "advertising_eligibility": [{"programType": "PROMOTED_LISTINGS_STANDARD", "status": "ELIGIBLE"}],
        }
        self.assertTrue(seller_ready(account))
        self.assertTrue(standard_ads_eligible(account))
        account["privileges"]["sellingLimit"]["quantity"] = 0
        self.assertFalse(seller_ready(account))

    def test_variant_rank_and_candidate_ledger_are_retained(self):
        ranked = select_variants({"combinations": [
            {"id": "a", "aliexpress_popularity": 1}, {"id": "b", "aliexpress_popularity": 9},
        ]})
        self.assertEqual({item["id"] for item in ranked["selected"]}, {"a", "b"})
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            append_record(ledger, {
                "record_type": "candidate", "canonical_url": "https://www.aliexpress.us/item/1005000000000001.html",
                "query_or_subcategory": "test", "gate_reached": "visible_price", "status": "accepted", "reason": "qualified",
            })
            self.assertEqual(summarize(ledger)["accepted_count"], 1)

    def test_browser_budget_caps_remain_hard(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "budget.json"
            initialize(state)
            consume(state, "source", browser_calls=60, dom_snapshots=12, browser_timeouts=3)
            with self.assertRaisesRegex(BudgetError, "budget_exceeded"):
                consume(state, "api", browser_calls=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
