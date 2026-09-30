"""Type checks for the discount card device APIs' `request_data`."""

from rest_framework import serializers

from core.api import REQUIRED


class CardUsageRequest(serializers.Serializer):
    """The customer, by full number: country code then number, digits only --
    the same string as Customer.mobile_full and the customer APIs'
    full_number."""

    full_number = serializers.RegexField(
        r"^\d+$", max_length=30,
        error_messages={**REQUIRED, "invalid": "must be digits only",
                        "max_length": "must be at most 30 characters"},
    )


class CardDiscountsRequest(serializers.Serializer):
    """No fields -- the company is the session's own, and every card type,
    grade and discount it has is sent whole, as it is; the device picks."""
