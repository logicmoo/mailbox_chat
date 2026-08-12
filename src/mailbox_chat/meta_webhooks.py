"""Shared verification helpers for signed Meta platform webhooks."""

from __future__ import annotations

import hashlib
import hmac


def verify_challenge(query: dict[str, list[str]], expected_token: str) -> str:
    mode = query.get("hub.mode", [""])[0]
    supplied = query.get("hub.verify_token", [""])[0]
    challenge = query.get("hub.challenge", [""])[0]
    if mode != "subscribe" or not expected_token or not hmac.compare_digest(supplied, expected_token):
        raise ValueError("Meta webhook verification failed")
    return challenge


def verify_signature(body: bytes, signature: str, app_secret: str) -> bool:
    if not app_secret or not signature.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)
