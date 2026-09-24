"""Type checks for the fare API's `request_data`. Same pattern as
apps/portal/serializers.py -- types only; which vehicle types a device may
ask for is decided in apps/fare/services.py::device_fares."""

from rest_framework import serializers

from core.api import whole_number_field


class FaresRequest(serializers.Serializer):
    vehicle_type_id = whole_number_field()
