"""Bodies as they are stored: secrets masked, size capped.

A log that holds passwords or live tokens is a breach waiting to happen, so any
key that names one is masked at every depth before a body is kept. JSON bodies
are masked structurally; anything else (a malformed body, which is exactly when
someone reads the log) falls back to masking "key": "value" pairs by pattern.
"""

import json
import re

from django.conf import settings

MASK = "***"
# A key is secret if it contains any of these (case-insensitive): "password",
# "new_password", "refresh_token", "tokens", "access"...
SECRET_PARTS = ("password", "passwd", "token", "secret", "authorization")
# Whole key names only: "pin" and "otp" are inside harmless words ("mapping").
SECRET_EXACT = {"access", "refresh", "otp", "pin"}
_TEXT_SECRET = re.compile(
    r'("(?:[^"]*(?:password|passwd|token|secret|authorization)[^"]*|access|refresh|otp|pin)"\s*:\s*)'
    r'("(?:[^"\\]|\\.)*"|[^,}\]\s]+)',
    re.IGNORECASE,
)


def is_secret(key):
    key = str(key).lower()
    return key in SECRET_EXACT or any(part in key for part in SECRET_PARTS)


def masked(value):
    if isinstance(value, dict):
        return {k: (MASK if is_secret(k) else masked(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v) for v in value]
    return value


def body_text(raw):
    """(stored text, truncated?) for a raw body (bytes or str)."""
    if not raw:
        return "", False
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        text = json.dumps(masked(json.loads(raw)), ensure_ascii=False, indent=1)
    except ValueError:
        text = _TEXT_SECRET.sub(lambda m: f'{m.group(1)}"{MASK}"', raw)
    limit = settings.MONITORING_BODY_LIMIT
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True
