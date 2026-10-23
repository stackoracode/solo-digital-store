"""OAuth 2.0 / OpenID Connect clients via Authlib (Google, optional generic OIDC such as Logto).

Authlib handles state, nonce, PKCE-less code flow with ID token validation against the provider JWKS.
Providers are enabled only when their client credentials exist in the environment.
"""

from __future__ import annotations

from functools import lru_cache

from authlib.integrations.starlette_client import OAuth

from app.config import get_settings

GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"


@lru_cache
def oauth_registry() -> OAuth:
    s = get_settings()
    oauth = OAuth()
    if s.google_client_id and s.google_client_secret:
        oauth.register(
            name="google",
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
            server_metadata_url=GOOGLE_METADATA,
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )
    if s.oidc_name and s.oidc_metadata_url and s.oidc_client_id:
        oauth.register(
            name=s.oidc_name,
            client_id=s.oidc_client_id,
            client_secret=s.oidc_client_secret,
            server_metadata_url=s.oidc_metadata_url,
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )
    return oauth


def enabled_providers() -> list[dict[str, str]]:
    s = get_settings()
    out = []
    if s.google_client_id and s.google_client_secret:
        out.append({"name": "google", "label": "Google"})
    if s.oidc_name and s.oidc_metadata_url and s.oidc_client_id:
        out.append({"name": s.oidc_name, "label": s.oidc_display_name})
    return out


def provider_key(name: str) -> str:
    return "google" if name == "google" else f"oidc:{name}"
