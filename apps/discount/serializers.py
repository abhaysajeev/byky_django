"""Type checks for the discount card device APIs' `request_data`."""

from rest_framework import serializers


class CardDiscountsRequest(serializers.Serializer):
    """No fields -- the company is the session's own, and every card type,
    grade and discount it has is sent whole, as it is; the device picks."""
