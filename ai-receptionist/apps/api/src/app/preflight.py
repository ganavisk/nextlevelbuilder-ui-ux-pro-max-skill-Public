"""`make check-keys`: confirm every credential works before dialling in.

A wrong key does not fail loudly on a phone call. The call just goes quiet,
the agent says "I'll have someone follow up", or nothing reaches the API at
all. This makes one cheap, read-only request to each provider with the
configured key (the Voyage check embeds a single word, since Voyage has no
free read endpoint) and says which one is wrong.

Keys are never printed, only their last four characters, so the output is
safe to paste into a bug report.

Run: python -m app.preflight   (or `make check-keys` from the repo root)
"""

import os
import sys
from dataclasses import dataclass
from typing import Literal

import httpx
from cryptography.fernet import Fernet

from app.core.config import ENV_FILES, Settings, get_settings

Status = Literal["ok", "fail", "warn", "skip"]

ANTHROPIC_API = "https://api.anthropic.com"
VAPI_API = "https://api.vapi.ai"
VOYAGE_API = "https://api.voyageai.com"
ELEVENLABS_API = "https://api.elevenlabs.io"
RESEND_API = "https://api.resend.com"
TWILIO_API = "https://api.twilio.com"

# The voices the example tenants are configured with (config/tenants/*.yaml).
TENANT_VOICES = {"dental (Diane)": "u91OxrFDaRINovYRRue2", "legal (Mike)": "xM0tQVvjAbr3ksKlp1EE"}


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    required: bool = False


def _tail(secret: str) -> str:
    return f"key ...{secret[-4:]}" if len(secret) > 8 else "key set"


def _rejected(response: httpx.Response) -> str:
    if response.status_code in (401, 403):
        return f"rejected (HTTP {response.status_code}): the key is wrong, revoked or lacks access"
    return f"unexpected HTTP {response.status_code}"


def _unreachable(host: str, exc: httpx.HTTPError) -> str:
    return f"could not reach {host} ({type(exc).__name__})"


# ── required for a test call ─────────────────────────────────────────────


def check_webhook_secret(settings: Settings) -> Check:
    name = "Vapi webhook secret"
    secret = settings.vapi_webhook_secret
    if not secret:
        return Check(
            name, "fail", "VAPI_WEBHOOK_SECRET is empty; every webhook would be refused", True
        )
    if len(secret) < 16:
        return Check(name, "warn", "set, but short; use `openssl rand -hex 32`", True)
    return Check(name, "ok", "set (checked by Vapi on every webhook once provisioned)", True)


def check_vapi(settings: Settings, client: httpx.Client) -> Check:
    name = "Vapi API key"
    key = settings.vapi_api_key
    if not key:
        return Check(
            name, "fail", "VAPI_API_KEY is empty; `make provision` cannot create the agent", True
        )
    try:
        r = client.get(
            f"{VAPI_API}/assistant", params={"limit": 1}, headers={"Authorization": f"Bearer {key}"}
        )
    except httpx.HTTPError as exc:
        return Check(name, "fail", _unreachable("api.vapi.ai", exc), True)
    if r.status_code == 200:
        return Check(name, "ok", f"accepted ({_tail(key)})", True)
    return Check(name, "fail", _rejected(r), True)


def check_anthropic(settings: Settings, client: httpx.Client) -> list[Check]:
    name = "Anthropic API key"
    key = settings.anthropic_api_key
    if not key:
        return [Check(name, "fail", "ANTHROPIC_API_KEY is empty; the agent cannot answer", True)]
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    checks: list[Check] = []
    models = [("in-call", settings.llm_model_primary), ("summaries", settings.llm_model_fast)]
    for purpose, model in models:
        label = f"Claude model for {purpose}"
        try:
            r = client.get(f"{ANTHROPIC_API}/v1/models/{model}", headers=headers)
        except httpx.HTTPError as exc:
            return [Check(name, "fail", _unreachable("api.anthropic.com", exc), True)]
        if r.status_code in (401, 403):
            return [Check(name, "fail", _rejected(r), True)]
        if r.status_code == 404:
            detail = f"{model!r} is not a model this key can use; check LLM_MODEL_*"
            checks.append(Check(label, "fail", detail, True))
        elif r.status_code == 200:
            checks.append(Check(label, "ok", f"{model} available", True))
        else:
            checks.append(Check(label, "fail", _rejected(r), True))
    return [Check(name, "ok", f"accepted ({_tail(key)})", True), *checks]


def check_public_url(settings: Settings, client: httpx.Client) -> Check:
    name = "Public URL to this API"
    url = settings.public_webhook_base_url.rstrip("/")
    if not url:
        detail = "PUBLIC_WEBHOOK_BASE_URL is empty; Vapi has nowhere to send the call"
        return Check(name, "fail", detail, True)
    if not url.startswith("https://"):
        return Check(name, "fail", f"{url} must be https:// (Vapi will not call plain http)", True)
    try:
        # ngrok's free tier interposes a browser warning page unless asked not to.
        r = client.get(f"{url}/healthz", headers={"ngrok-skip-browser-warning": "1"})
    except httpx.HTTPError as exc:
        return Check(name, "fail", f"{_unreachable(url, exc)}; is the tunnel running?", True)
    if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
        try:
            healthy = r.json().get("status") == "ok"
        except ValueError:
            healthy = False
        if healthy:
            return Check(name, "ok", f"{url} reaches this API", True)
    return Check(
        name,
        "fail",
        f"{url}/healthz answered HTTP {r.status_code} but not as this API; "
        "is `make dev` running and the tunnel pointed at port 8000?",
        True,
    )


# ── optional: features degrade without them ──────────────────────────────


def check_voyage(settings: Settings, client: httpx.Client) -> Check:
    name = "Voyage API key"
    key = settings.voyage_api_key
    if not key:
        return Check(
            name, "warn", "not set: questions get the follow-up fallback instead of an answer"
        )
    try:
        r = client.post(
            f"{VOYAGE_API}/v1/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"input": ["ping"], "model": settings.embedding_model, "input_type": "query"},
        )
    except httpx.HTTPError as exc:
        return Check(name, "fail", _unreachable("api.voyageai.com", exc))
    if r.status_code == 200:
        return Check(name, "ok", f"accepted ({_tail(key)}); run `make seed` after setting it")
    return Check(name, "fail", _rejected(r))


def check_elevenlabs(client: httpx.Client) -> list[Check]:
    # The app never uses this key: Vapi calls ElevenLabs with the key stored in
    # the Vapi dashboard. If it is in the environment we can at least confirm
    # the configured voices exist on that account.
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        return [
            Check(
                "ElevenLabs voices",
                "skip",
                "add the key in Vapi -> Provider Keys; set ELEVENLABS_API_KEY here to check voices",
            )
        ]
    checks = []
    for label, voice_id in TENANT_VOICES.items():
        name = f"ElevenLabs voice, {label}"
        try:
            r = client.get(f"{ELEVENLABS_API}/v1/voices/{voice_id}", headers={"xi-api-key": key})
        except httpx.HTTPError as exc:
            checks.append(Check(name, "fail", _unreachable("api.elevenlabs.io", exc)))
            continue
        if r.status_code == 200:
            checks.append(Check(name, "ok", f"{voice_id} found"))
        elif r.status_code in (400, 404):
            checks.append(Check(name, "fail", f"{voice_id} not found on this account"))
        else:
            checks.append(Check(name, "fail", _rejected(r)))
    return checks


def check_encryption_key(settings: Settings) -> Check:
    name = "Credentials encryption key"
    key = settings.credentials_encryption_key
    if not key:
        return Check(
            name,
            "warn",
            "not set: needed before connecting Google Calendar or HubSpot (generate one: "
            "python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())')",
        )
    try:
        Fernet(key.encode())
    except ValueError:
        return Check(name, "fail", "set, but not a valid Fernet key (32 url-safe base64 bytes)")
    return Check(name, "ok", "valid")


def check_oauth_pair(label: str, client_id: str, client_secret: str, feature: str) -> Check:
    if not client_id and not client_secret:
        return Check(label, "skip", f"not set: {feature}")
    if not (client_id and client_secret):
        return Check(label, "fail", "only one of the client id / secret is set")
    return Check(label, "ok", "set (checked when you complete the connect flow)")


def check_resend(settings: Settings, client: httpx.Client) -> Check:
    name = "Resend (email)"
    key = settings.resend_api_key
    if not key:
        return Check(name, "skip", "not set: no confirmation emails")
    try:
        r = client.get(f"{RESEND_API}/domains", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return Check(name, "fail", _unreachable("api.resend.com", exc))
    if r.status_code == 200:
        return Check(name, "ok", f"accepted ({_tail(key)})")
    return Check(name, "fail", _rejected(r))


def check_twilio(settings: Settings, client: httpx.Client) -> Check:
    name = "Twilio (SMS)"
    sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    if not sid and not token:
        return Check(name, "skip", "not set: no confirmation texts")
    if not (sid and token):
        return Check(name, "fail", "only one of TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN is set")
    try:
        r = client.get(f"{TWILIO_API}/2010-04-01/Accounts/{sid}.json", auth=(sid, token))
    except httpx.HTTPError as exc:
        return Check(name, "fail", _unreachable("api.twilio.com", exc))
    if r.status_code == 200:
        return Check(name, "ok", "accepted")
    return Check(name, "fail", _rejected(r))


# ── run + report ─────────────────────────────────────────────────────────


def run_checks(settings: Settings, client: httpx.Client) -> list[Check]:
    return [
        check_webhook_secret(settings),
        check_vapi(settings, client),
        *check_anthropic(settings, client),
        check_public_url(settings, client),
        check_voyage(settings, client),
        *check_elevenlabs(client),
        check_encryption_key(settings),
        check_oauth_pair(
            "Google Calendar OAuth",
            settings.google_oauth_client_id,
            settings.google_oauth_client_secret,
            "the agent takes callback details instead of booking",
        ),
        check_oauth_pair(
            "HubSpot OAuth",
            settings.hubspot_client_id,
            settings.hubspot_client_secret,
            "calls are not synced to a CRM",
        ),
        check_resend(settings, client),
        check_twilio(settings, client),
    ]


_LABEL = {"ok": "OK  ", "fail": "FAIL", "warn": "WARN", "skip": "--  "}


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = ["Reading settings from:"]
    for path in ENV_FILES:
        lines.append(f"  {'found  ' if path.exists() else 'missing'} {path}")
    lines.append("")
    for heading, required in (("Needed for a test call", True), ("Optional", False)):
        lines.append(heading)
        for c in (c for c in checks if c.required is required):
            lines.append(f"  {_LABEL[c.status]}  {c.name.ljust(width)}  {c.detail}")
        lines.append("")
    blocking = [c for c in checks if c.required and c.status == "fail"]
    if blocking:
        lines.append(
            f"Not ready: fix the {len(blocking)} FAIL line(s) under 'Needed for a test call'."
        )
    else:
        lines.append("Ready to provision: `make seed`, `make provision`, then call the number.")
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.required and c.status == "fail" for c in checks) else 0


def main() -> int:
    settings = get_settings()
    with httpx.Client(timeout=10.0) as client:
        checks = run_checks(settings, client)
    print(render(checks))
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
