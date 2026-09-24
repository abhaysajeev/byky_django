"""The operator app's vehicle download -- views only translate HTTP; the rules
are in apps/fleet/services.py::device_vehicles.

POST /api/v1/{app}/vehicles: every vehicle at the caller's own station, nested
category -> vehicle type -> vehicles. The station comes from the login session.
"""

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.fleet import services
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses


class VehiclesRequest(serializers.Serializer):
    """Nothing to send: the station is the session's."""


_VEHICLES_SAMPLE = {
    "generated_at": "2026-09-24T06:00:12+04:00",
    "branch": {"id": 12, "code": "ADC1", "name": "Abu Dhabi Corniche 1"},
    "total_vehicles": 3,
    "categories": [
        {
            "category_id": 1, "code": "byky", "name": "BYKY",
            "vehicle_types": [
                {
                    "vehicle_type_id": 7, "code": "", "name": "Monaco", "brand": "BERG", "tax_percentage": "5.00",
                    "vehicles": [
                        {"vehicle_id": 1041, "vehicle_code": "MON-0041", "vehicle_name": "Monaco 41",
                         "rfid_epc": "E28011606000020B1C2D3E41", "uom": "Number", "is_available": True},
                        {"vehicle_id": 1042, "vehicle_code": "MON-0042", "vehicle_name": "Monaco 42",
                         "rfid_epc": "", "uom": "Number", "is_available": False},
                    ],
                },
                {
                    "vehicle_type_id": 4, "code": "", "name": "Berg", "brand": "BERG", "tax_percentage": "5.00",
                    "vehicles": [
                        {"vehicle_id": 2007, "vehicle_code": "BRG-0007", "vehicle_name": "Berg 7",
                         "rfid_epc": "E28011606000020B1C2D4F07", "uom": "Number", "is_available": True},
                    ],
                },
            ],
        },
    ],
}

_DESCRIPTION = """
Every vehicle at the caller's own station, nested **category -> vehicle type -> vehicles**.
The station comes from the login session; `request_data` can be empty.

- Only active, approved vehicles (and active, approved types and categories).
- `is_available: false` vehicles are included, so the app can say why one cannot be rented.
- `vehicle_type_id` is the id `POST /api/v1/operator/fares` takes.
- `rfid_epc` may be `""` (no tag assigned yet): also allow lookup by `vehicle_code`.
- Categories and vehicle types with no vehicle at this station are left out.
"""


class VehiclesView(APIView):
    """POST /api/v1/{app}/vehicles -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Vehicles"],
        summary="Every vehicle at this station, by category and vehicle type",
        description=_DESCRIPTION,
        request=envelope_request("VehiclesEnvelope", VehiclesRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Vehicles.", _VEHICLES_SAMPLE),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "device_not_mapped", "This device has no station.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        branch = request.auth.branch
        if branch is None:
            return envelope("device_not_mapped", "This device has no station.", http_status=409)
        return envelope("ok", "Vehicles.", services.device_vehicles(branch))
