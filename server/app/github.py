"""GitHub App client for /class.

Gær talks to GitHub as a GitHub App installed on the teacher's organisation:
  - app JWT (RS256, signed with GITHUB_APP_PRIVATE_KEY)  → installation tokens, installation lookups
  - installation token                                    → teams, invitations, repos, rights
  - user authorisation (OAuth of the same app)            → proves which GitHub account a Gær user owns and
                                                            which installations they can reach
A GitHub account is only ever linked to a Gær user; it never logs anyone in.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app import config

# Tests replace this with an httpx.MockTransport.
transport: httpx.AsyncBaseTransport | None = None

_tokens: dict[int, tuple[str, float]] = {}  # installation id → (token, expires at)


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    s = config.settings
    return bool(s.github_app_id and s.github_app_slug and s.github_app_private_key
                and s.github_client_id and s.github_client_secret)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _json(data: dict) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode()


def app_jwt(now: float | None = None) -> str:
    s = config.settings
    issued = int(now or time.time())
    signing_input = f"{_b64(_json({'alg': 'RS256', 'typ': 'JWT'}))}." \
                    f"{_b64(_json({'iat': issued - 60, 'exp': issued + 540, 'iss': s.github_app_id}))}"
    key = serialization.load_pem_private_key((s.github_app_private_key or "").replace("\\n", "\n").encode(), password=None)
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    return f"{signing_input}.{_b64(signature)}"


async def request(method: str, url: str, *, token: str | None = None, body: dict | None = None,
                  form: dict | None = None) -> tuple[int, Any]:
    if url.startswith("/"):
        url = config.settings.github_api_url.rstrip("/") + url
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "gaer-classroom"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30, connect=10)) as client:
            r = await client.request(method, url, headers=headers, json=body, data=form)
    except httpx.HTTPError as e:
        raise GitHubError(0, f"GitHub svarede ikke ({type(e).__name__})") from e
    data = None
    if r.content:
        try:
            data = r.json()
        except ValueError:
            data = None
    return r.status_code, data


def fail(status: int, data: Any, what: str) -> GitHubError:
    message = data.get("message") if isinstance(data, dict) else None
    return GitHubError(status, f"{what}: {message or f'HTTP {status}'}")


# ── As the app and as an installation ──────────────────────────────────────

async def get_installation(installation_id: int) -> dict:
    status, data = await request("GET", f"/app/installations/{installation_id}", token=app_jwt())
    if status != 200:
        raise fail(status, data, "Installationen blev ikke fundet")
    return data


async def installation_token(installation_id: int) -> str:
    cached = _tokens.get(installation_id)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    status, data = await request("POST", f"/app/installations/{installation_id}/access_tokens", token=app_jwt())
    if status != 201:
        raise fail(status, data, "Kunne ikke få adgang til organisationen")
    _tokens[installation_id] = (data["token"], time.time() + 50 * 60)  # GitHub tokens live one hour
    return data["token"]


async def call(installation_id: int, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    return await request(method, path, token=await installation_token(installation_id), body=body)


# ── User authorisation (links a GitHub account, never logs in) ─────────────

def redirect_uri() -> str:
    return config.settings.origin.rstrip("/") + "/api/class/github/callback"


def authorize_url(state: str) -> str:
    s = config.settings
    return f"{s.github_web_url}/login/oauth/authorize?" + urlencode(
        {"client_id": s.github_client_id, "redirect_uri": redirect_uri(), "state": state, "allow_signup": "true"})


def install_url(state: str) -> str:
    s = config.settings
    return f"{s.github_web_url}/apps/{s.github_app_slug}/installations/new?" + urlencode({"state": state})


async def exchange_code(code: str) -> str:
    s = config.settings
    status, data = await request("POST", f"{s.github_web_url}/login/oauth/access_token", form={
        "client_id": s.github_client_id, "client_secret": s.github_client_secret, "code": code,
        "redirect_uri": redirect_uri()})
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        reason = data.get("error_description") if isinstance(data, dict) else None
        raise GitHubError(status, f"GitHub afviste koden: {reason or f'HTTP {status}'}")
    return data["access_token"]


async def get_user(user_token: str) -> dict:
    status, data = await request("GET", "/user", token=user_token)
    if status != 200:
        raise fail(status, data, "Kunne ikke læse din GitHub-konto")
    return data


async def user_installation_ids(user_token: str) -> set[int]:
    status, data = await request("GET", "/user/installations?per_page=100", token=user_token)
    if status != 200:
        raise fail(status, data, "Kunne ikke læse dine installationer")
    return {int(i["id"]) for i in data.get("installations", [])}


# ── Webhooks ───────────────────────────────────────────────────────────────

def valid_signature(body: bytes, header: str | None) -> bool:
    secret = config.settings.github_webhook_secret
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
