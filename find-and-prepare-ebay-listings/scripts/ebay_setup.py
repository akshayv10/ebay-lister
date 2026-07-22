#!/usr/bin/env python3
"""Configure and verify the private Production eBay seller API connection."""

from __future__ import annotations

import argparse
import getpass
import json
from datetime import datetime, timezone
from typing import Any

from ebay_common import (
    CAMPAIGN_NAME,
    CONFIG_PATH,
    LOCATION_KEY,
    LOCALE,
    MARKETPLACE,
    PROMOTED_RATE,
    ApiError,
    EbayClient,
    EbayError,
    Keychain,
    UnknownOutcome,
    consent_url,
    exchange_authorization_code,
    load_config,
    prompt_credentials,
    public_status,
    save_config,
)


def records(payload: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    value = payload.get(key, [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def policy_allows_standard(policy: dict[str, Any]) -> bool:
    categories = policy.get("categoryTypes", [])
    return any(
        isinstance(item, dict) and str(item.get("name", "")) == "ALL_EXCLUDING_MOTORS_VEHICLES"
        for item in categories if isinstance(categories, list)
    )


def policy_summary(payload: Any, key: str, id_key: str) -> list[dict[str, Any]]:
    return [
        {"id": str(item.get(id_key, "")), "name": str(item.get("name", ""))}
        for item in records(payload, key) if item.get(id_key) and policy_allows_standard(item)
    ]


def fulfillment_is_free(policy: dict[str, Any]) -> bool:
    for option in policy.get("shippingOptions", []) if isinstance(policy.get("shippingOptions"), list) else []:
        if not isinstance(option, dict) or str(option.get("optionType", "")).upper() != "DOMESTIC":
            continue
        for service in option.get("shippingServices", []) if isinstance(option.get("shippingServices"), list) else []:
            if not isinstance(service, dict):
                continue
            cost = service.get("shippingCost", {})
            if service.get("freeShipping") is True or (isinstance(cost, dict) and str(cost.get("value", "")) in {"0", "0.0", "0.00"}):
                return True
    return False


def fetch_account(client: EbayClient) -> dict[str, Any]:
    privileges = client.request("GET", "/sell/account/v1/privilege").data
    eligibility = client.request(
        "GET", "/sell/account/v1/advertising_eligibility",
        query={"program_types": "PROMOTED_LISTINGS_STANDARD"}, marketplace_header=True,
    ).data
    programs = client.request("GET", "/sell/account/v1/program/get_opted_in_programs").data
    payments = client.request("GET", "/sell/account/v1/payment_policy", query={"marketplace_id": MARKETPLACE}).data
    returns = client.request("GET", "/sell/account/v1/return_policy", query={"marketplace_id": MARKETPLACE}).data
    fulfillment = client.request("GET", "/sell/account/v1/fulfillment_policy", query={"marketplace_id": MARKETPLACE}).data
    locations = client.request("GET", "/sell/inventory/v1/location", query={"limit": 100}).data
    campaigns = client.request(
        "GET", "/sell/marketing/v1/ad_campaign", query={"limit": 200, "offset": 0}, marketplace_header=True
    ).data
    return {
        "privileges": privileges if isinstance(privileges, dict) else {},
        "advertising_eligibility": records(eligibility, "advertisingEligibility"),
        "programs": records(programs, "programs"),
        "payment_policies": policy_summary(payments, "paymentPolicies", "paymentPolicyId"),
        "return_policies": policy_summary(returns, "returnPolicies", "returnPolicyId"),
        "fulfillment_policies": [
            {
                "id": str(item.get("fulfillmentPolicyId", "")),
                "name": str(item.get("name", "")),
                "free_domestic_shipping": fulfillment_is_free(item),
            }
            for item in records(fulfillment, "fulfillmentPolicies")
            if item.get("fulfillmentPolicyId") and policy_allows_standard(item)
        ],
        "locations": [
            {
                "merchant_location_key": str(item.get("merchantLocationKey", "")),
                "name": str(item.get("name", "")),
                "status": str(item.get("merchantLocationStatus", "")),
            }
            for item in records(locations, "locations")
        ],
        "campaigns": [
            {
                "campaign_id": str(item.get("campaignId", "")),
                "name": str(item.get("campaignName", "")),
                "status": str(item.get("campaignStatus", "")),
                "funding_model": str(item.get("fundingStrategy", {}).get("fundingModel", "")),
                "bid_percentage": str(item.get("fundingStrategy", {}).get("bidPercentage", "")),
            }
            for item in records(campaigns, "campaigns")
        ],
    }


def opted_in(account: dict[str, Any]) -> bool:
    return any(str(item.get("programType", "")).upper() == "SELLING_POLICY_MANAGEMENT" for item in account["programs"])


def seller_ready(account: dict[str, Any]) -> bool:
    privileges = account["privileges"]
    if privileges.get("sellerRegistrationCompleted") is not True:
        return False
    limit = privileges.get("sellingLimit")
    if not isinstance(limit, dict):
        return True
    quantity = limit.get("quantity")
    amount = limit.get("amount", {})
    try:
        quantity_ready = quantity is None or int(quantity) > 0
        amount_ready = not isinstance(amount, dict) or amount.get("value") is None or float(amount["value"]) > 0
    except (TypeError, ValueError):
        return False
    return quantity_ready and amount_ready


def standard_ads_eligible(account: dict[str, Any]) -> bool:
    return any(
        str(item.get("programType", "")) == "PROMOTED_LISTINGS_STANDARD"
        and str(item.get("status", "")) == "ELIGIBLE"
        for item in account["advertising_eligibility"]
    )


def exact_campaigns(account: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in account["campaigns"]
        if item["name"] == CAMPAIGN_NAME
        and item["funding_model"] == "COST_PER_SALE"
        and item["bid_percentage"] in {"10", "10.0", "10.00"}
        and item["status"] in {"RUNNING", "SCHEDULED"}
    ]


def preflight(client: EbayClient) -> dict[str, Any]:
    account = fetch_account(client)
    config = load_config()
    free_ids = {item["id"] for item in account["fulfillment_policies"] if item["free_domestic_shipping"]}
    result = {
        "status": "ready" if all((
            seller_ready(account),
            standard_ads_eligible(account),
            opted_in(account),
            bool(account["payment_policies"]),
            bool(account["return_policies"]),
            bool(free_ids),
            any(item["merchant_location_key"] == LOCATION_KEY and item["status"] == "ENABLED" for item in account["locations"]),
            bool(exact_campaigns(account)),
            config.get("payment_policy_id") in {item["id"] for item in account["payment_policies"]},
            config.get("return_policy_id") in {item["id"] for item in account["return_policies"]},
            config.get("fulfillment_policy_id") in free_ids,
            config.get("campaign_id") in {item["campaign_id"] for item in exact_campaigns(account)},
        )) else "setup_required",
        "seller_registration_ready": account["privileges"].get("sellerRegistrationCompleted") is True,
        "selling_limit": account["privileges"].get("sellingLimit"),
        "general_ads_eligible": standard_ads_eligible(account),
        "selling_policy_management": opted_in(account),
        "payment_policies": account["payment_policies"],
        "return_policies": account["return_policies"],
        "fulfillment_policies": account["fulfillment_policies"],
        "inventory_location_ready": any(
            item["merchant_location_key"] == LOCATION_KEY and item["status"] == "ENABLED" for item in account["locations"]
        ),
        "general_campaign_10_percent_ready": bool(exact_campaigns(account)),
        "priority_campaign_created_by_tool": False,
        "config_path": str(CONFIG_PATH),
    }
    return result


def select_policy(items: list[dict[str, Any]], requested: str, label: str, require_free: bool = False) -> str:
    eligible = [item for item in items if not require_free or item.get("free_domestic_shipping") is True]
    if requested:
        if requested not in {item["id"] for item in eligible}:
            raise EbayError(f"Selected {label} is unavailable or incompatible: {requested}")
        return requested
    if len(eligible) == 1:
        return str(eligible[0]["id"])
    if not eligible:
        raise EbayError(f"No compatible {label} exists in EBAY_US")
    raise EbayError(f"Multiple {label} values exist; rerun with the desired policy ID")


def prompt_location_address() -> dict[str, str]:
    """Collect the dispatch address without echoing or persisting it locally."""
    prompts = (
        ("addressLine1", "Dispatch street address"),
        ("city", "Dispatch city"),
        ("stateOrProvince", "Dispatch state code"),
        ("postalCode", "Dispatch postal code"),
        ("country", "Dispatch country code"),
    )
    address = {key: getpass.getpass(f"{label} (input hidden): ").strip() for key, label in prompts}
    if any(not value for value in address.values()):
        raise EbayError("Every dispatch-address field is required")
    address["country"] = address["country"].upper()
    if address["country"] != "US":
        raise EbayError("This EBAY_US workflow requires a US dispatch address")
    return address


def configure_account(args: argparse.Namespace, client: EbayClient) -> dict[str, Any]:
    if not args.apply:
        raise EbayError("configure-account requires --apply because it changes the seller account")
    account = fetch_account(client)
    if not opted_in(account):
        try:
            client.request(
                "POST", "/sell/account/v1/program/opt_in",
                json_body={"programType": "SELLING_POLICY_MANAGEMENT"}, expected=(204,),
            )
        except UnknownOutcome:
            if not opted_in(fetch_account(client)):
                raise
        account = fetch_account(client)
    payment_id = select_policy(account["payment_policies"], args.payment_policy_id, "payment policy")
    return_id = select_policy(account["return_policies"], args.return_policy_id, "return policy")
    fulfillment_id = select_policy(
        account["fulfillment_policies"], args.fulfillment_policy_id, "free-shipping fulfillment policy", require_free=True
    )

    if not any(item["merchant_location_key"] == LOCATION_KEY for item in account["locations"]):
        address = prompt_location_address()
        try:
            client.request(
                "POST", f"/sell/inventory/v1/location/{LOCATION_KEY}", expected=(204,),
                json_body={
                    "location": {"address": address},
                    "locationTypes": ["WAREHOUSE"],
                    "merchantLocationStatus": "ENABLED",
                    "name": "API dispatch location",
                },
            )
        except UnknownOutcome:
            refreshed = fetch_account(client)
            if not any(item["merchant_location_key"] == LOCATION_KEY for item in refreshed["locations"]):
                raise
    elif not any(item["merchant_location_key"] == LOCATION_KEY and item["status"] == "ENABLED" for item in account["locations"]):
        client.request("POST", f"/sell/inventory/v1/location/{LOCATION_KEY}/enable", expected=(204,))

    account = fetch_account(client)
    campaigns = exact_campaigns(account)
    if len(campaigns) > 1:
        raise EbayError(f"More than one active {CAMPAIGN_NAME!r} campaign exists; resolve the duplicate before setup")
    if campaigns:
        campaign_id = campaigns[0]["campaign_id"]
    else:
        try:
            response = client.request(
                "POST", "/sell/marketing/v1/ad_campaign", expected=(201,), marketplace_header=True,
                json_body={
                    "campaignName": CAMPAIGN_NAME,
                    "fundingStrategy": {
                        "adRateStrategy": "FIXED",
                        "bidPercentage": PROMOTED_RATE,
                        "fundingModel": "COST_PER_SALE",
                    },
                    "marketplaceId": MARKETPLACE,
                    "startDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
            location = response.headers.get("Location") or response.headers.get("location") or ""
            campaign_id = location.rstrip("/").rsplit("/", 1)[-1]
        except UnknownOutcome:
            reconciled = exact_campaigns(fetch_account(client))
            if len(reconciled) != 1:
                raise
            campaign_id = reconciled[0]["campaign_id"]
        if not campaign_id or campaign_id == "ad_campaign":
            raise EbayError("Campaign outcome is ambiguous; run preflight and reconcile before retrying")

    config = {
        "marketplace_id": MARKETPLACE,
        "locale": LOCALE,
        "merchant_location_key": LOCATION_KEY,
        "payment_policy_id": payment_id,
        "return_policy_id": return_id,
        "fulfillment_policy_id": fulfillment_id,
        "campaign_id": campaign_id,
        "campaign_name": CAMPAIGN_NAME,
        "promoted_rate_percent": PROMOTED_RATE,
    }
    save_config(config)
    return {"status": "configured", **{key: value for key, value in config.items() if key != "oauth_site_base_url"}}


def authorize() -> dict[str, Any]:
    keychain = Keychain()
    prompt_credentials(keychain)
    url = consent_url(keychain)
    print(json.dumps({"status": "consent_required", "consent_url": url}))
    callback = getpass.getpass("After approving in eBay, paste the complete accepted-page URL (input hidden), or press Return to finish later: ").strip()
    if not callback:
        return {"status": "consent_pending", "consent_url": url}
    exchange_authorization_code(callback, keychain)
    return {"status": "authorized", "seller_authorized": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("authorize")
    sub.add_parser("reauthorize")
    sub.add_parser("preflight")
    configure = sub.add_parser("configure-account")
    configure.add_argument("--apply", action="store_true")
    configure.add_argument("--payment-policy-id", default="")
    configure.add_argument("--return-policy-id", default="")
    configure.add_argument("--fulfillment-policy-id", default="")
    args = parser.parse_args()
    try:
        if args.command == "status":
            payload = public_status()
        elif args.command in {"authorize", "reauthorize"}:
            payload = authorize()
        elif args.command == "preflight":
            payload = preflight(EbayClient())
        else:
            payload = configure_account(args, EbayClient())
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("status") not in {"setup_required"} else 3
    except (EbayError, ApiError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
