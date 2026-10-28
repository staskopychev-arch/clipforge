import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.app_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8")


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise ValueError("Admin password must be at least 12 characters")
    actual_salt = salt or os.urandom(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=actual_salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${base64.urlsafe_b64encode(actual_salt).decode()}${base64.urlsafe_b64encode(derived).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_text, digest_text = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _session_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app_secret, salt="admin-session")


def create_session(username: str) -> str:
    return _session_signer().dumps({"username": username})


def verify_session(token: str) -> bool:
    if not token:
        return False
    try:
        payload = _session_signer().loads(token, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return False
    return hmac.compare_digest(str(payload.get("username", "")), settings.admin_username)

