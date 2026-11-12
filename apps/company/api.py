"""The operator app's station downloads -- views only translate HTTP, the
rules are in apps/company/services.py::branch_details, device_payment_modes.

POST /api/v1/{app}/branch: this station's own details.
POST /api/v1/{app}/payment-modes: this station's company's active payment
modes, for the checkout screen. Call it at login and whenever the app
refreshes; each answer replaces what the app holds.
POST /api/v1/{app}/dial-codes: the countries for a mobile number's code, for
the operator app (apps/company/dial_codes.py).
POST /api/v1/{app}/company: the company the tablet is registered to, every
field except the logo (apps/company/services.py::company_details).
"""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company import dial_codes, services
from apps.company.models import DialCode
from apps.company.serializers import (
    BranchDetailsRequest,
    CompanyDetailsRequest,
    DialCodesRequest,
    PaymentModesRequest,
)
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, other_app, request_parts, session_station
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


_DIAL_CODES_SAMPLE = {
    "version": "2026-11-12T10:00:00+04:00",
    "primary": [
        {"iso2": "AE", "country": "United Arab Emirates", "flag": "\U0001f1e6\U0001f1ea", "dial_code": "+971",
         "dial_code_digits": 3, "mobile_digits_min": 9, "mobile_digits_max": 9, "example": "501234567"},
    ],
    "secondary": [
        {"iso2": "DE", "country": "Germany", "flag": "\U0001f1e9\U0001f1ea", "dial_code": "+49",
         "dial_code_digits": 2, "mobile_digits_min": 10, "mobile_digits_max": 11, "example": "15123456789"},
    ],
}

_DIAL_CODES_DESCRIPTION = """
The countries a mobile number can belong to, for the phone field: name, flag,
dialling code and how many digits the number has **after** the code.

**Request** (`request_data`, may be empty or left out): nothing to send. The
company comes from the session.

**Sent:** two lists (owner, 12 Nov 2026).
* `primary` -- the company's primary dial codes, in the order its admin set
  them (Company -> Configurations). Show these first. Empty when none are set.
* `secondary` -- every other active country, A-Z.

Per country: `iso2` (ISO code), `country`, `flag` (emoji), `dial_code` (with
`+`), `dial_code_digits` (digits in the code, without `+`),
`mobile_digits_min` / `mobile_digits_max` (digits after the code; equal unless
the country has more than one length -- Germany 10-11) and `example` (a
sample mobile number, digits after the code). Several countries share a code
(`+1` USA, Canada and the Caribbean; `+7`; `+44`): each is its own row. For a
`+1` country the 3-digit area code is part of the number.

`version` changes whenever either list does: keep your copy until it differs.
Source: Google's libphonenumber and Unicode CLDR, kept on our server.
"""


class DialCodesView(APIView):
    """POST /api/v1/{app}/dial-codes -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Dial Codes"],
        summary="Countries and their dial codes, primary first",
        description=_DIAL_CODES_DESCRIPTION,
        request=envelope_request("DialCodesEnvelope", DialCodesRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Dial codes.", _DIAL_CODES_SAMPLE),
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
        form = DialCodesRequest(data=request_data)
        form.is_valid(raise_exception=True)
        return envelope("ok", "Dial codes.", dial_codes.device_dial_codes(DialCode, branch.company))


# -- The company -------------------------------------------------------------------------

_COMPANY_SAMPLE = {"company": {
    "id": 1, "code": "BYKY", "name": "BY KY SPORT & LEISURE EQUIPMENT RENTAL & TRADING LLC",
    "timezone": "Asia/Dubai", "utc_offset": "+04:00",
    "ceo_name": "Ahmed Al Mansoori", "dto_name": "", "date_of_commissioning": "2019-03-01",
    "address": {"address": "Office 12, Al Quoz Industrial 3", "city": "Dubai", "state_id": 2, "state": "Dubai",
                "country_id": 1, "country": "United Arab Emirates", "zip_code": ""},
    "registration": {"incorporation_certificate_number": "", "business_certificate_number": "CN-1234567",
                     "income_tax_number": "100123456700003", "tin": "", "cst": "", "service_tax_number": ""},
    "tax": {"tax_percentage": "5.00", "tax_type": "included", "discount_type": "before_tax"},
    "contact": {"contact_person": "Mohammed", "phone_number": "+97142345678", "fax_number": "",
                "email": "ops@byky.ae", "website": "https://byky.ae"},
    "version": "2026-11-12 18:40:11",
}}

_COMPANY_DESCRIPTION = """
The company this tablet is registered to: every field of the web's Company
screen except the logo. Call it at login and whenever the app refreshes; each
answer replaces what the app holds. `version` moves whenever the company is
saved on the web, so an unchanged `version` means nothing changed.

**Request** (`request_data`, may be empty or left out): nothing to send. The
company is always the registered device's own, never a value the app sends.

**Reply**, grouped as on the web:
- `income_tax_number` is the tax registration number printed on invoices
  (the TRN in the UAE).
- `tax.tax_percentage` is a 2-decimal string. `tax.tax_type`: `included`
  (fares already contain the tax) or `excluded` (tax is added on top).
  `tax.discount_type`: `before_tax` or `after_tax`.
- `utc_offset` is the company timezone's offset now, e.g. `+04:00`.
- Empty text comes back as `""`; an unset `date_of_commissioning` as null.
  No field is ever left out.
"""


class CompanyDetailsView(APIView):
    """POST /api/v1/{app}/company -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Company"],
        summary="The company this tablet is registered to",
        description=_COMPANY_DESCRIPTION,
        request=envelope_request("CompanyDetailsEnvelope", CompanyDetailsRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Company.", _COMPANY_SAMPLE),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "device_not_registered", "This device is not registered.", {}),
            (409, "company_inactive", "This company is switched off.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        refused = other_app(request, app)
        if refused:
            return refused
        device = getattr(request.auth, "device", None)
        if device is None or device.company_id is None:
            return envelope("device_not_registered", "This device is not registered.", http_status=409)

        _, request_data = request_parts(request)
        form = CompanyDetailsRequest(data=request_data)
        form.is_valid(raise_exception=True)

        company = type(device.company).objects.select_related("state", "country").get(pk=device.company_id)
        if not company.is_active:
            return envelope("company_inactive", "This company is switched off.", http_status=409)
        return envelope("ok", "Company.", {"company": services.company_details(company)})
