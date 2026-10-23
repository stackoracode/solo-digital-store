"""Password hashing, opaque tokens, field encryption, fingerprints, TOTP and CSRF helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import lru_cache

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings

_hasher = PasswordHasher()
# Pre-computed hash so failed lookups take the same time as real verifications.
_DUMMY_HASH = _hasher.hash("timing-equaliser")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def keyed_hash(value: str, purpose: str = "fp") -> str:
    key = get_settings().fingerprint_key.encode()
    return hmac.new(key, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


@lru_cache
def _fernet() -> MultiFernet:
    settings = get_settings()
    keys = [k.strip() for k in settings.data_encryption_keys.split(",") if k.strip()]
    if not keys:
        if settings.is_production:
            raise RuntimeError("DATA_ENCRYPTION_KEYS is required")
        # Deterministic dev/test key derived from SECRET_KEY; never used in production.
        import base64

        keys = [base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()).decode()]
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - indicates key misconfiguration
        raise RuntimeError("Unable to decrypt value; check DATA_ENCRYPTION_KEYS") from exc


def mask_secret(value: str, keep: int = 4) -> str:
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * min(8, len(value) - keep) + value[-keep:]


# TOTP ------------------------------------------------------------------------------------------


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=get_settings().app_name)


def verify_totp(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


# CSRF ------------------------------------------------------------------------------------------
# The csrf cookie holds a random nonce; forms carry HMAC(secret, nonce). An attacker able to plant
# a cookie still cannot forge the matching form token without the server secret.


def csrf_token_for(nonce: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"csrf:{nonce}".encode(), hashlib.sha256).hexdigest()


def csrf_valid(nonce: str | None, token: str | None) -> bool:
    if not nonce or not token:
        return False
    return hmac.compare_digest(csrf_token_for(nonce), token)
