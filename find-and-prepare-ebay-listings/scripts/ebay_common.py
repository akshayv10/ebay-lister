#!/usr/bin/env python3
"""Shared, redaction-safe eBay OAuth, Keychain, configuration, and HTTP helpers."""

from __future__ import annotations

import base64
import getpass
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


MARKETPLACE = "EBAY_US"
LOCALE = "en-US"
LOCATION_KEY = "irvine-92618"
CAMPAIGN_NAME = "Codex API Listings 10 Percent"
PROMOTED_RATE = "10.0"
KEYCHAIN_SERVICE = "find-and-prepare-ebay-listings.production"
# On the local Mac secrets live in Keychain and config in Application Support.
# In CI (GitHub Actions) there is no Keychain: set EBAY_ACCOUNT_CONFIG to point at
# the repo-committed non-secret account.json, and provide the four secrets as the
# environment variables mapped in ENV_SECRET_VARS below.
DEFAULT_CONFIG_PATH = Path.home() / "Library" / "Application Support" / "find-and-prepare-ebay-listings" / "account.json"
CONFIG_PATH = Path(os.environ["EBAY_ACCOUNT_CONFIG"]) if os.environ.get("EBAY_ACCOUNT_CONFIG") else DEFAULT_CONFIG_PATH
SCOPES = (
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/sell.metadata",
)
SECRET_ACCOUNTS = ("client_id", "client_secret", "runame", "refresh_token")
# Environment-variable fallback for each Keychain-backed secret, used when running
# unattended (e.g. GitHub Actions) where macOS Keychain is unavailable. When the
# variable is present and non-empty it takes precedence over Keychain.
ENV_SECRET_VARS = {
    "client_id": "EBAY_CLIENT_ID",
    "client_secret": "EBAY_CLIENT_SECRET",
    "runame": "EBAY_RUNAME",
    "refresh_token": "EBAY_REFRESH_TOKEN",
}


class EbayError(RuntimeError):
    pass


class UnknownOutcome(EbayError):
    """A mutating request may have reached eBay, so retrying is unsafe."""


class SecretMissing(EbayError):
    pass


class ApiError(EbayError):
    def __init__(self, method: str, url: str, status: int, payload: Any):
        self.method = method
        self.url = url
        self.status = status
        self.payload = payload
        super().__init__(f"eBay API {method} {safe_url(url)} failed with HTTP {status}: {error_summary(payload)}")


def safe_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    safe_query = []
    for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        safe_query.append((key, "[redacted]" if key.casefold() in {"code", "refresh_token", "access_token"} else item))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(safe_query), ""))


def error_summary(payload: Any) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            return str(first.get("longMessage") or first.get("message") or first.get("errorId") or "request failed")[:500]
        return str(payload.get("error_description") or payload.get("error") or payload.get("message") or "request failed")[:500]
    return str(payload or "request failed")[:500]


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EbayError(f"Could not read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EbayError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = read_json(path)
    forbidden = {"client_id", "client_secret", "refresh_token", "access_token", "address", "addressLine1"}
    if forbidden.intersection(value):
        raise EbayError("Local account config contains a forbidden secret or full address")
    return value


def save_config(value: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    allowed = {
        "marketplace_id", "locale", "merchant_location_key", "payment_policy_id",
        "return_policy_id", "fulfillment_policy_id", "campaign_id", "campaign_name",
        "promoted_rate_percent", "oauth_site_base_url",
    }
    unexpected = set(value) - allowed
    if unexpected:
        raise EbayError(f"Refusing to persist unexpected config fields: {sorted(unexpected)}")
    write_json(path, value)


class Keychain:
    def __init__(self, service: str = KEYCHAIN_SERVICE, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None):
        self.service = service
        self.runner = runner or subprocess.run

    def get(self, account: str) -> str:
        env_name = ENV_SECRET_VARS.get(account)
        if env_name:
            env_value = os.environ.get(env_name, "").strip()
            if env_value:
                return env_value
        result = self.runner(
            ["/usr/bin/security", "find-generic-password", "-s", self.service, "-a", account, "-w"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise SecretMissing(f"Missing secret: {account} (set env {env_name or account.upper()} or store it in macOS Keychain)")
        return result.stdout.strip()

    def has(self, account: str) -> bool:
        try:
            self.get(account)
            return True
        except SecretMissing:
            return False

    def set(self, account: str, value: str) -> None:
        if account not in SECRET_ACCOUNTS:
            raise EbayError(f"Unsupported Keychain account: {account}")
        if not value.strip():
            raise EbayError(f"Refusing to store an empty Keychain value: {account}")
        result = self.runner(
            ["/usr/bin/security", "add-generic-password", "-U", "-s", self.service, "-a", account, "-w"],
            input=value + "\n", capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise EbayError(f"Could not store {account} in macOS Keychain")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    data: Any
    url: str


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        form_body: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> Response:
        request_headers = dict(headers or {})
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        elif form_body is not None:
            body = urllib.parse.urlencode(form_body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
            raw = response.read()
            status = int(response.status)
            response_headers = {key: value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = int(exc.code)
            response_headers = {key: value for key, value in exc.headers.items()}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UnknownOutcome(f"Network outcome is unknown for {method} {safe_url(url)}: {exc}") from exc
        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = raw.decode("utf-8", errors="replace")[:2000]
        else:
            data = None
        return Response(status=status, headers=response_headers, data=data, url=url)


class EbayClient:
    def __init__(self, keychain: Keychain | None = None, transport: Any | None = None):
        self.keychain = keychain or Keychain()
        self.transport = transport or UrllibTransport()
        self._access_token = ""
        self._expires_at = 0.0

    @property
    def api_base(self) -> str:
        return "https://api.ebay.com"

    def access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        client_id = self.keychain.get("client_id")
        client_secret = self.keychain.get("client_secret")
        refresh_token = self.keychain.get("refresh_token")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        response = self.transport.request(
            "POST",
            f"{self.api_base}/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}"},
            form_body={"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": " ".join(SCOPES)},
        )
        if response.status != 200 or not isinstance(response.data, dict) or not response.data.get("access_token"):
            raise ApiError("POST", response.url, response.status, response.data)
        self._access_token = str(response.data["access_token"])
        self._expires_at = time.time() + int(response.data.get("expires_in", 7200))
        return self._access_token

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: Any | None = None,
        expected: tuple[int, ...] = (200, 201, 204),
        marketplace_header: bool = False,
    ) -> Response:
        url = path_or_url if path_or_url.startswith("https://") else f"{self.api_base}{path_or_url}"
        if query:
            encoded = urllib.parse.urlencode({key: value for key, value in query.items() if value is not None})
            url = f"{url}{'&' if '?' in url else '?'}{encoded}"
        headers = {
            "Authorization": f"Bearer {self.access_token()}",
            "Accept": "application/json",
            "Accept-Language": LOCALE,
            "Content-Language": LOCALE,
        }
        if marketplace_header:
            headers["X-EBAY-C-MARKETPLACE-ID"] = MARKETPLACE
        response = self.transport.request(method, url, headers=headers, json_body=json_body)
        if response.status not in expected:
            raise ApiError(method, response.url, response.status, response.data)
        return response


def consent_url(keychain: Keychain | None = None) -> str:
    secrets = keychain or Keychain()
    query = urllib.parse.urlencode({
        "client_id": secrets.get("client_id"),
        "redirect_uri": secrets.get("runame"),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "prompt": "login",
    })
    return f"https://auth.ebay.com/oauth2/authorize?{query}"


def authorization_code(callback_url: str) -> str:
    parsed = urllib.parse.urlsplit(callback_url.strip())
    values = urllib.parse.parse_qs(parsed.query)
    code = (values.get("code") or [""])[0].strip()
    if not code and callback_url.strip() and "//" not in callback_url:
        code = callback_url.strip()
    if not code:
        raise EbayError("The accepted callback URL does not contain an authorization code")
    return code


def exchange_authorization_code(callback_url: str, keychain: Keychain | None = None, transport: Any | None = None) -> None:
    secrets = keychain or Keychain()
    http = transport or UrllibTransport()
    client_id = secrets.get("client_id")
    client_secret = secrets.get("client_secret")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = http.request(
        "POST",
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}"},
        form_body={
            "grant_type": "authorization_code",
            "code": authorization_code(callback_url),
            "redirect_uri": secrets.get("runame"),
        },
    )
    if response.status != 200 or not isinstance(response.data, dict) or not response.data.get("refresh_token"):
        raise ApiError("POST", response.url, response.status, response.data)
    secrets.set("refresh_token", str(response.data["refresh_token"]))


def prompt_credentials(keychain: Keychain | None = None) -> None:
    secrets = keychain or Keychain()
    prompts = (
        ("client_id", "Production Client ID", False),
        ("client_secret", "Production Client Secret", True),
        ("runame", "Production RuName", False),
    )
    for account, label, hidden in prompts:
        if secrets.has(account):
            continue
        value = getpass.getpass(f"{label}: ") if hidden else input(f"{label}: ")
        secrets.set(account, value.strip())


def public_status(config_path: Path = CONFIG_PATH, keychain: Keychain | None = None) -> dict[str, Any]:
    secrets = keychain or Keychain()
    config = load_config(config_path)
    return {
        "environment": "production",
        "credentials_configured": all(secrets.has(item) for item in ("client_id", "client_secret", "runame")),
        "seller_authorized": secrets.has("refresh_token"),
        "account_configured": all(config.get(item) for item in (
            "payment_policy_id", "return_policy_id", "fulfillment_policy_id", "merchant_location_key", "campaign_id"
        )),
        "marketplace_id": config.get("marketplace_id", MARKETPLACE),
        "merchant_location_key": config.get("merchant_location_key", ""),
        "campaign_name": config.get("campaign_name", ""),
        "promoted_rate_percent": config.get("promoted_rate_percent", ""),
    }
