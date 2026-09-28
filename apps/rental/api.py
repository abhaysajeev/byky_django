"""The operator app's customer lookup -- views only translate HTTP; the rules
are in apps/rental/services.py::customer_by_phone.

POST /api/v1/{app}/customers/lookup: given a phone number, the matching
customer's data -- so the operator app can prefill a rental for a returning
customer instead of retyping their details.
"""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.portal.authentication import AppJWTAuthentication
from apps.rental import services
from apps.rental.serializers import CustomerLookupRequest
from core.api import envelope, request_parts, session_station
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses

_CUSTOMER_SAMPLE = {
    "customer_id": 4021, "customer_code": "CU014", "first_name": "Ahmed", "last_name": "Al Mansoori",
    "gender": "male", "date_of_birth": "1990-05-14", "nationality": "United Arab Emirates",
    "id_type": "emirates_id", "id_no": "784-1990-1234567-1",
    "mobile_country_code": "+971", "mobile_no": "501234567", "email": "ahmed.almansoori@example.com",
    "address": "Dubai, UAE", "is_blocked": False, "block_reason": "",
}

_CUSTOMER_BLOCKED_SAMPLE = {
    **_CUSTOMER_SAMPLE, "customer_id": 4022, "customer_code": "CU015", "first_name": "Fatima",
    "last_name": "Hassan", "mobile_no": "501234568", "is_blocked": True,
    "block_reason": "Repeated late returns",
}

_DESCRIPTION = """
The customer matching a phone number -- so the operator app can prefill a rental
for a returning customer instead of retyping their details.

**Request** (`request_data`): `mobile_no` required -- the local number only, no
country code (what a station operator types or scans).

**Sent:** the customer's full record, **including a blocked one** -- `is_blocked`
and `block_reason` tell the app the state, but the lookup itself is never
refused for a blocked customer. It is scoped to this device's own company even
though `mobile_no` is globally unique, so one company's device can never reach
another company's customer by guessing a phone number.

An unknown phone number is `unknown_customer`.
"""


class CustomerLookupView(APIView):
    """POST /api/v1/{app}/customers/lookup -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Customers"],
        summary="The customer matching a phone number",
        description=_DESCRIPTION,
        request=envelope_request("CustomerLookupEnvelope", CustomerLookupRequest),
        responses=envelope_responses(
            (200, "ok", "Customer.", _CUSTOMER_SAMPLE),
            (200, "ok", "Customer.", _CUSTOMER_BLOCKED_SAMPLE, "ok (blocked)"),
            (400, "invalid_request", "mobile_no is required.", {"errors": {"mobile_no": "is required"}}),
            (404, "unknown_customer", "No customer with that phone number.", {}),
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
        form = CustomerLookupRequest(data=request_data)
        form.is_valid(raise_exception=True)

        try:
            customer = services.customer_by_phone(branch.company, form.validated_data["mobile_no"])
        except services.UnknownCustomer:
            return envelope("unknown_customer", "No customer with that phone number.", http_status=404)
        return envelope("ok", "Customer.", _customer_json(customer))


def _customer_json(customer):
    return {
        "customer_id": customer.pk, "customer_code": customer.customer_code,
        "first_name": customer.first_name, "last_name": customer.last_name,
        "gender": customer.gender, "date_of_birth": customer.date_of_birth.isoformat()
        if customer.date_of_birth else None,
        "nationality": customer.nationality, "id_type": customer.id_type, "id_no": customer.id_no,
        "mobile_country_code": customer.mobile_country_code, "mobile_no": customer.mobile_no,
        "email": customer.email, "address": customer.address,
        "is_blocked": customer.is_blocked, "block_reason": customer.block_reason,
    }
