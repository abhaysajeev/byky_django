"""The operator app's vehicle download -- views only translate HTTP; the rules
are in apps/fleet/services.py::device_vehicles.

POST /api/v1/{app}/vehicles: every vehicle at the caller's own station, nested
category -> vehicle type -> vehicles, optionally narrowed to one category or
vehicle type. The station comes from the login session, never from the app
(core/api.py::session_station).
"""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.fleet import services
from apps.fleet.serializers import VehiclesRequest
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, request_parts, session_station
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses

# The 200 example Swagger shows: a whole station, no filter -- one category,
# two vehicle types in name order, vehicles in identifier order.
_VEHICLES_SAMPLE = {
    "generated_at": "2026-09-24T06:00:12+04:00",
    "branch": {"id": 12, "code": "ADC1", "name": "Abu Dhabi Corniche 1"},
    "filters": {"vehicle_category_id": None, "vehicle_type_id": None},
    "total_vehicles": 3,
    "categories": [
        {
            "category_id": 1, "code": "BYKY", "name": "BYKY", "vehicle_count": 3,
            "vehicle_types": [
                {
                    "vehicle_type_id": 4, "code": "BRG", "name": "Berg", "brand": "BERG", "tax_percentage": "5.00",
                    "vehicle_count": 1,
                    "vehicles": [
                        {"vehicle_id": 2007, "vehicle_identifier": "VB0321", "vehicle_code": "BRG07",
                         "vehicle_name": "BRG 07", "rfid_epc": "35440575", "uom": "Number", "is_available": True},
                    ],
                },
                {
                    "vehicle_type_id": 7, "code": "MON", "name": "Monaco", "brand": "BERG", "tax_percentage": "5.00",
                    "vehicle_count": 2,
                    "vehicles": [
                        {"vehicle_id": 1041, "vehicle_identifier": "VB1241", "vehicle_code": "MO41",
                         "vehicle_name": "MO 41", "rfid_epc": "36543595", "uom": "Number", "is_available": True},
                        {"vehicle_id": 1042, "vehicle_identifier": "VB1242", "vehicle_code": "MO42",
                         "vehicle_name": "MO 42", "rfid_epc": "", "uom": "Number", "is_available": False},
                    ],
                },
            ],
        },
    ],
}

_DESCRIPTION = """
Every vehicle at this station, nested **category -> vehicle type -> vehicles**. Call it at
login and whenever the app refreshes its fleet; each answer **replaces** what the app holds
for the same filters.

**Request** (`request_data`, may be empty or left out): `vehicle_category_id` and
`vehicle_type_id`, both optional -- each only narrows the answer; both together must both
match. **The station is never sent**: it is the one this device was mapped to at login,
taken from the access token's session.

**Sent:**
- Only active, approved vehicles, of active, approved vehicle types and categories.
- `is_available: false` vehicles are included, so the app can say why one cannot be rented.
- Categories and vehicle types with no vehicle here are left out; `vehicle_count` is the
  number of vehicles listed under each, `total_vehicles` the number in the answer.
- `filters` echoes what was applied (`null` = not filtered).

**Fields:**
- `vehicle_identifier` (`VB0042`) is the server's own number for the vehicle: unique, never
  edited, never reused. Vehicles are listed in its order within each type.
- `vehicle_type_id` is the id `POST /api/v1/operator/fares` takes.
- `rfid_epc` may be `""` (no tag assigned yet): also allow lookup by `vehicle_code`.

A category or vehicle type this station cannot use (another company's, inactive, not
approved) is an error; a usable one with no vehicle here is an empty list.
"""


class VehiclesView(APIView):
    """POST /api/v1/{app}/vehicles -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Vehicles"],
        summary="The vehicles at this station, by category and vehicle type",
        description=_DESCRIPTION,
        request=envelope_request("VehiclesEnvelope", VehiclesRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Vehicles.", _VEHICLES_SAMPLE),
            (400, "invalid_request", "vehicle_type_id must be a whole number.",
             {"errors": {"vehicle_type_id": "must be a whole number"}}),
            (400, "unknown_vehicle_category", "That vehicle category is not set up for this station.", {}),
            (400, "unknown_vehicle_type", "That vehicle type is not set up for this station.", {}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "device_not_mapped", "This device has no station.", {}),
            (409, "branch_inactive", "This station is closed.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        branch, refused = session_station(request)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = VehiclesRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data

        try:
            data = services.device_vehicles(branch, vehicle_category_id=values.get("vehicle_category_id"),
                                            vehicle_type_id=values.get("vehicle_type_id"))
        except services.UnknownCategory:
            return envelope("unknown_vehicle_category", "That vehicle category is not set up for this station.",
                            http_status=400)
        except services.UnknownVehicleType:
            return envelope("unknown_vehicle_type", "That vehicle type is not set up for this station.",
                            http_status=400)
        return envelope("ok", "Vehicles.", data)
