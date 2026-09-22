"""Type checks for the device API's `request_data`.

Types only -- present, text, a whole number. Business rules live in services.py.
A wrong type answers 400 naming the field, so an app bug shows up the first time
the app is tested instead of being silently answered "no update" forever.
"""

from rest_framework import serializers

from apps.devices.models import Platform

from core.api import REQUIRED, text_field, whole_number_field


class UpdateCheckRequest(serializers.Serializer):
    """design/03-login.md section 9A.2. Unknown keys are dropped."""

    installation_id = text_field(64)
    # Only read for a tablet the server has never seen: a known one carries its
    # company on its own row.
    company_id = text_field(10, required=False, allow_blank=True, allow_null=True)
    version_code = whole_number_field(required=False, allow_null=True)
    version_name = text_field(20, required=False, allow_blank=True, allow_null=True)


class RegistrationRequest(serializers.Serializer):
    """design/registration/registration-api.md section 4. Unknown keys dropped;
    no branch or username is ever read from a registration.

    `company_id` is the one thing the app does say about where it belongs: one
    server hosts several companies, and before approval a tablet has nothing
    else that could name one. It is matched on `Company.short_code` and taken as
    sent -- approval is the control (services.company_for_code).
    """

    installation_id = text_field(64)
    company_id = text_field(10, required=False, allow_blank=True, allow_null=True)
    platform = serializers.ChoiceField(
        choices=Platform.choices,
        error_messages={**REQUIRED, "invalid_choice": "must be android or ios"},
    )
    platform_id = text_field(64, required=False, allow_blank=True, allow_null=True)
    device_model = text_field(120, required=False, allow_blank=True, allow_null=True)
