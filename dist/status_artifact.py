"""Authenticated Oracle 961 status artifacts.

The public ``stat.i`` object is ciphertext.  Only the web process and the
hourly publisher receive ``ORACLE_STATUS_KEY``.  The decrypted document does
not contain account, payment, or credential data.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


FORMAT = "dli-oracle961-status-fernet-gzip-v1"


class StatusArtifactError(ValueError):
    """Raised when an artifact cannot be authenticated or decoded."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def validate_key(key: str | bytes) -> bytes:
    raw = key.encode("ascii") if isinstance(key, str) else key
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception as exc:  # pragma: no cover - exact decoder exception varies
        raise StatusArtifactError("ORACLE_STATUS_KEY is not valid url-safe base64") from exc
    if len(decoded) != 32:
        raise StatusArtifactError("ORACLE_STATUS_KEY must encode exactly 32 bytes")
    return raw


def key_id(key: str | bytes) -> str:
    raw = validate_key(key)
    return hashlib.sha256(base64.urlsafe_b64decode(raw)).hexdigest()[:16]


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def encrypt_status(payload: dict[str, Any], key: str | bytes) -> bytes:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise StatusArtifactError("unsupported status payload")
    raw_key = validate_key(key)
    plaintext = _canonical(payload)
    envelope = {
        "ciphertext": Fernet(raw_key).encrypt(gzip.compress(plaintext, compresslevel=9, mtime=0)).decode("ascii"),
        "encoding": "gzip+json",
        "format": FORMAT,
        "keyId": key_id(raw_key),
        "payloadSha256": hashlib.sha256(plaintext).hexdigest(),
    }
    return _canonical(envelope) + b"\n"


def decrypt_status(artifact: bytes, key: str | bytes) -> dict[str, Any]:
    raw_key = validate_key(key)
    try:
        envelope = json.loads(artifact)
        if envelope.get("format") != FORMAT or envelope.get("keyId") != key_id(raw_key):
            raise StatusArtifactError("status artifact format or key identifier is invalid")
        compressed = Fernet(raw_key).decrypt(envelope["ciphertext"].encode("ascii"))
        if envelope.get("encoding") != "gzip+json":
            raise StatusArtifactError("status artifact encoding is invalid")
        plaintext = gzip.decompress(compressed)
        if not hashlib.sha256(plaintext).hexdigest() == envelope.get("payloadSha256"):
            raise StatusArtifactError("status payload checksum is invalid")
        payload = json.loads(plaintext)
    except (InvalidToken, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise StatusArtifactError("status artifact authentication failed") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise StatusArtifactError("unsupported status payload")
    return payload
