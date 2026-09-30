"""The operator app's station downloads -- views only translate HTTP, the
rules are in apps/company/services.py::branch_details, device_payment_modes.

POST /api/v1/{app}/branch: this station's own details.
POST /api/v1/{app}/payment-modes: this station's company's active payment
modes, for the checkout screen. Call it at login and whenever the app
refreshes; each answer replaces what the app holds.
"""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company import services
from apps.company.serializers import BranchDetailsRequest, PaymentModesRequest
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, request_parts, session_station
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses

_BRANCH_SAMPLE = {
    "id": 12, "code": "ADC1", "name": "Abu Dhabi Corniche 1", "branch_type": "station",
    "is_hotel": False, "accepts_app_payment": True, "address": "Corniche Road, Abu Dhabi",
    "latitude": "24.476900", "longitude": "54.354200", "contact_no": "+971501234567",
}

_BRANCH_DESCRIPTION = """
This station's own details -- name, address, contact, and the two toggles
(`is_hotel`, `accepts_app_payment`) the checkout screen reads. Call it at
login and whenever the app refreshes.

**Request** (`request_data`, may be empty or left out): nothing to send --
the branch is the signed-in session's own, never a value the app sends. A
device with no token cannot reach this endpoint: which station a tablet
belongs to is never revealed before login (`apps/devices/api.py::
RegistrationView`), and this one is no exception.
"""


class BranchDetailsView(APIView):
    """POST /api/v1/{app}/branch -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Branch"],
        summary="This station's own details",
        description=_BRANCH_DESCRIPTION,
        request=envelope_request("BranchDetailsEnvelope", BranchDetailsRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Branch.", _BRANCH_SAMPLE),
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
        form = BranchDetailsRequest(data=request_data)
        form.is_valid(raise_exception=True)

        return envelope("ok", "Branch.", services.branch_details(branch))

_PAYMENT_MODES_SAMPLE = {
    "payment_modes": [
        {"id": 1, "name": "Cash"},
        {"id": 3, "name": "Card"},
    ],
}

_DESCRIPTION = """
This station's company's active payment modes, for the checkout screen.

**Request** (`request_data`, may be empty or left out): nothing to send --
the company comes from the session, and the full active list is always
returned whole, the same "replaces what the app holds" contract as
`/vehicles` and `/fares`.

**Sent:** only active, approved payment modes (`apps/company/models.py::
PaymentMode`) -- a mode switched off (e.g. Cheque, Creditor) is left out, not
sent with a flag, since the app has nothing useful to do with an unusable
mode besides not showing it.
"""


class PaymentModesView(APIView):
    """POST /api/v1/{app}/payment-modes -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Payment Modes"],
        summary="This station's active payment modes",
        description=_DESCRIPTION,
        request=envelope_request("PaymentModesEnvelope", PaymentModesRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Payment modes.", _PAYMENT_MODES_SAMPLE),
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
        form = PaymentModesRequest(data=request_data)
        form.is_valid(raise_exception=True)

        modes = services.device_payment_modes(branch.company)
        return envelope("ok", "Payment modes.", {"payment_modes": modes})
