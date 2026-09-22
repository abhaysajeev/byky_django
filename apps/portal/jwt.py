"""Access/refresh tokens for the phone apps (design/03-login.md section 6).

Raw PyJWT, not djangorestframework-simplejwt's Token classes: those import
django.contrib.auth.models at module load, which this project deliberately
does not install (config/settings.py, REST_FRAMEWORK comment) -- confirmed by
trying it directly. PyJWT itself has no such dependency.
"""

import hashlib
import uuid
from datetime import timedelta

import jwt
from django.conf import settings
from django.utils import timezone

ALGORITHM = "HS256"
ACCESS_LIFETIME = timedelta(minutes=30)
REFRESH_LIFETIME = timedelta(days=30)


class TokenInvalid(Exception):
    """Bad signature, expired, or the wrong typ claim for where it was used."""


def _encode(user, session, *, typ, lifetime, now):
    exp = now + lifetime
    payload = {
        # "sub" is a registered claim PyJWT itself requires to be a string
        # (RFC 7519) -- a bare int decodes fine standalone but raises
        # InvalidSubjectError the moment PyJWT validates it, which every
        # caller here then sees only as a generic "malformed" token.
        "sub": str(user.pk),
        "sid": session.pk,
        "chan": session.channel,
        "typ": typ,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)
    return token, exp


def issue_access_token(user, session, *, now=None):
    return _encode(user, session, typ="access", lifetime=ACCESS_LIFETIME, now=now or timezone.now())


def issue_refresh_token(user, session, *, now=None):
    return _encode(user, session, typ="refresh", lifetime=REFRESH_LIFETIME, now=now or timezone.now())


def decode_token(token, *, expected_type):
    """The payload, or TokenInvalid -- never a raw PyJWT/jwt exception, so
    every caller catches one thing regardless of what went wrong."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise TokenInvalid("expired")
    except jwt.InvalidTokenError:
        raise TokenInvalid("malformed")
    if payload.get("typ") != expected_type:
        raise TokenInvalid("wrong_type")
    return payload


def hash_token(token):
    """For AppSession.refresh_token_hash -- the raw token is never stored."""
    return hashlib.sha256(token.encode()).hexdigest()
