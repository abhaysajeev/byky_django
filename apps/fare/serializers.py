"""Type checks for the fare API's `request_data`. Same pattern as
apps/portal/serializers.py -- types only; which vehicle types a device may
ask for is decided in apps/fare/services.py::device_fares."""

from rest_framework import serializers

from apps.fare.pricing import MAX_MINUTES
from core.api import date_field, whole_number_field


class FaresRequest(serializers.Serializer):
    date = date_field()
    vehicle_type_id = whole_number_field(required=False)
    package_minutes = whole_number_field(required=False, min_value=1, max_value=MAX_MINUTES)
