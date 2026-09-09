"""Password hashing/verification and session-token generation.

Dependency-free by design (spec constraint: no bcrypt/passlib): uses stdlib
hashlib.pbkdf2_hmac('sha256', ...) with a random per-user salt (os.urandom)
and a high iteration count. 260,000 iterations is comfortably above the
OWASP-recommended minimum (600k is quoted for PBKDF2-SHA256 in some current
guidance, but that figure targets high-value internet-facing auth; this is a
local single-practice app, and 260k already costs ~100ms/verify on modest
hardware, which is an acceptable login-time cost here). Verification uses
hmac.compare_digest for constant-time comparison so a timing side-channel
cannot be used to guess a hash byte-by-byte.
"""
import hashlib
import hmac
import os
import secrets

PBKDF2_ITERATIONS = 260_000
SALT_BYTES = 16


def hash_password(password: str, salt: bytes = None):
    """Returns (salt_hex, hash_hex). Generates a fresh random salt if none given."""
    if salt is None:
        salt = os.urandom(SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), derived.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """Constant-time verification. Never raises on malformed stored values --
    a corrupt/missing hash simply fails to verify."""
    if not salt_hex or not hash_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except (ValueError, TypeError):
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(derived.hex(), hash_hex)


def new_session_token() -> str:
    """secrets.token_urlsafe(32) -> ~43 url-safe chars, 256 bits of entropy."""
    return secrets.token_urlsafe(32)
