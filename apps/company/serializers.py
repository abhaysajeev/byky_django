"""Type checks for the company device APIs' `request_data`."""

from rest_framework import serializers


class PaymentModesRequest(serializers.Serializer):
    """No fields -- the station is never sent, it is the session's, and there
    is nothing else to narrow: a company's payment modes are a handful of
    rows, always sent whole."""


class BranchDetailsRequest(serializers.Serializer):
    """No fields -- the branch is the session's own (session_station), the
    same as PaymentModesRequest. Deliberately authenticated only: a device
    with no token cannot learn its branch this way -- see
    apps/devices/api.py::RegistrationView, which never reveals one either,
    "the station reaches the app only at login"."""
