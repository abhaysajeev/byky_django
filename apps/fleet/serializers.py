"""Type checks for the vehicles API's `request_data`. Same pattern as
apps/fare/serializers.py -- types only; which categories and vehicle types a
device may ask for is decided in apps/fleet/services.py::device_vehicles."""

from rest_framework import serializers

from core.api import whole_number_field


class VehiclesRequest(serializers.Serializer):
    """Both optional: each only narrows the answer. The station is never sent --
    it is the session's."""

    vehicle_category_id = whole_number_field(required=False)
    vehicle_type_id = whole_number_field(required=False)
