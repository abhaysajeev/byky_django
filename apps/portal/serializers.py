"""Type checks for the app-auth API's `request_data`. Same pattern as
apps/devices/serializers.py -- types only, business rules live elsewhere
(apps/portal/auth.py, apps/portal/jwt.py)."""

from rest_framework import serializers

from core.api import REQUIRED, text_field


class LoginRequest(serializers.Serializer):
    """design/03-login.md section 5.2/9.1. Shared by every channel's login --
    one login function, one request shape, per section 9.1's own rule.

    installation_id is optional here (type-checked only): whether it's
    actually required is a business rule, not a type, and differs by
    channel -- the employee channel has no device concept at all, the
    operator channel refuses without one. That's enforced in
    apps/portal/api.py, not here.
    """

    username = text_field(100)
    # trim_whitespace=False: a password is never silently altered before
    # being checked -- a leading/trailing space the user actually typed must
    # fail like any other wrong password, not be forgiven.
    # max_length=128: comfortably above any real password, but bounded --
    # without it, an arbitrarily large body gets run through the password
    # hasher before being rejected.
    password = serializers.CharField(
        trim_whitespace=False, max_length=128,
        error_messages={**REQUIRED, "max_length": "must be at most 128 characters"},
    )
    # Same max length as the registration/update-check contracts already use
    # for this field (apps/devices/serializers.py).
    installation_id = text_field(64, required=False, allow_blank=True)


class RefreshRequest(serializers.Serializer):
    """design/03-login.md section 6.3. Sent only here, never as a header."""

    refresh_token = text_field(2000)


class ServerTimeRequest(serializers.Serializer):
    """design/registration/server-time-api.md section 3. Unlike registration and
    the update check, `company_id` is required here -- several companies share
    this database, so there is no single-company deployment to guess when it
    is missing (apps/portal/api.py::ServerTimeView)."""

    company_id = text_field(10)
