"""Type checks for the company device APIs' `request_data`."""

from rest_framework import serializers


class PaymentModesRequest(serializers.Serializer):
    """No fields -- the station is never sent, it is the session's, and there
    is nothing else to narrow: a company's payment modes are a handful of
    rows, always sent whole."""
