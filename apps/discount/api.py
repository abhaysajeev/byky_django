"""The discount card device APIs -- views only translate HTTP.

POST /api/v1/{app}/card-discounts: every card type of the device's company,
the grades under each, and each grade's discount configuration -- sent as it
is (active or not, any validity); the device picks what applies.

POST /api/v1/{app}/card-discounts/usage: a customer's (by full number)
redemptions of each discount usable today; the device applies the window.
"""

from django.db.models import Prefetch
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company.models import WeekDay
from apps.discount import services
from apps.discount.models import CardDiscount, CardGrade, CardType
from apps.discount.serializers import (
    ApprovalRequest,
    ApprovalStatusRequest,
    CardDiscountsRequest,
    CardUsageRequest,
)
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, request_parts, session_station
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import zone_for


def _days(percent_by_day):
    return [{"weekday": day, "day": WeekDay(day).label, "discount_percent": percent}
            for day, percent in percent_by_day]


_CARD_DISCOUNTS_SAMPLE = {
    "card_types": [
        {
            "card_type_id": 3, "code": "CORP", "name": "Corporate", "is_active": True,
            "grades": [
                {
                    "card_grade_id": 7, "code": "GOLD", "name": "Gold", "is_active": True,
                    "discounts": [
                        {
                            "card_discount_id": 12, "valid_from": "2026-09-01", "valid_to": "2026-12-31",
                            "is_active": True, "requires_approval": True, "fare_basis": "base",
                            "usage_type": "per_week", "usage_limit": 2,
                            "days": _days([(day, "15.00") for day in range(7)]),
                        },
                    ],
                },
                {"card_grade_id": 9, "code": "PLAT", "name": "Platinum", "is_active": True, "discounts": []},
                {
                    "card_grade_id": 8, "code": "SILV", "name": "Silver", "is_active": True,
                    "discounts": [
                        {
                            "card_discount_id": 13, "valid_from": "2026-10-01", "valid_to": "2026-12-31",
                            "is_active": True, "requires_approval": False, "fare_basis": "full",
                            "usage_type": "one_time", "usage_limit": None,
                            "days": _days([(4, "20.00"), (5, "25.00")]),
                        },
                    ],
                },
            ],
        },
    ],
}

_DESCRIPTION = """
Every card type of this device's company, the grades under each, and each
grade's discount configuration -- **as it is**: inactive rows, grades with no
discount and discounts of any validity are all included, each with its own
`is_active` and dates. The device picks what applies. No `request_data`.

* `requires_approval` -- `false`: apply directly; `true`: ask for approval first.
* `fare_basis` -- `base` or `full`: the fare the % is taken from.
* `usage_type` -- `one_time` (`usage_limit` null), `per_day`, `per_week`,
  `per_month`, `per_period`; `usage_limit` counts per customer.
* `days` -- only the days the discount applies on; a missing day has none.
  `weekday`: Monday = 0 … Sunday = 6.
* Dates `YYYY-MM-DD`; `discount_percent` a string with 2 decimals.
"""


class CardDiscountsView(APIView):
    """POST /api/v1/{app}/card-discounts -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Discount Cards"],
        summary="Every card type, its grades and their discounts",
        description=_DESCRIPTION,
        request=envelope_request("CardDiscountsEnvelope", CardDiscountsRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Card discounts.", _CARD_DISCOUNTS_SAMPLE),
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
        CardDiscountsRequest(data=request_data).is_valid(raise_exception=True)

        return envelope("ok", "Card discounts.", {"card_types": card_types_json(branch.company)})


def card_types_json(company):
    """The whole tree for one company, in four queries whatever its size."""
    discounts = CardDiscount.objects.order_by("-valid_from", "pk").prefetch_related("days")
    grades = CardGrade.objects.order_by("name", "pk").prefetch_related(Prefetch("discounts", queryset=discounts))
    types = (CardType.objects.filter(company=company).order_by("name", "pk")
             .prefetch_related(Prefetch("grades", queryset=grades)))
    return [
        {
            "card_type_id": card_type.pk, "code": card_type.code, "name": card_type.name,
            "is_active": card_type.is_active,
            "grades": [
                {
                    "card_grade_id": grade.pk, "code": grade.code, "name": grade.name,
                    "is_active": grade.is_active,
                    "discounts": [_discount_json(discount) for discount in grade.discounts.all()],
                }
                for grade in card_type.grades.all()
            ],
        }
        for card_type in types
    ]


def _discount_json(discount):
    return {
        "card_discount_id": discount.pk,
        "valid_from": discount.valid_from.isoformat(), "valid_to": discount.valid_to.isoformat(),
        "is_active": discount.is_active,
        "requires_approval": discount.requires_approval, "fare_basis": discount.fare_basis,
        "usage_type": discount.usage_type, "usage_limit": discount.usage_limit,
        "days": _days((day.weekday, str(day.discount_percent)) for day in discount.days.all()),
    }


# -- Customer card usage -----------------------------------------------------------

_CARD_USAGE_SAMPLE = {
    "customer": {"customer_id": 4021, "customer_code": "CU014", "name": "Ahmed Ali"},
    "grades": [
        {
            "card_type_id": 3, "card_type_name": "Corporate", "card_grade_id": 7, "card_grade_name": "Gold",
            "card_discount_id": 12, "usage_type": "per_week", "usage_limit": 2,
            "used": 3, "redemptions": ["2026-09-12 11:20:05", "2026-09-27 10:04:00", "2026-09-29 18:30:12"],
        },
        {
            "card_type_id": 3, "card_type_name": "Corporate", "card_grade_id": 8, "card_grade_name": "Silver",
            "card_discount_id": 13, "usage_type": "one_time", "usage_limit": None,
            "used": 0, "redemptions": [],
        },
    ],
}

_USAGE_DESCRIPTION = """
How much a customer has used each card discount that can be used today.
`request_data`: `full_number` -- country code then number, digits only
(`971501234567`).

One entry per discount that is active and valid today (its card type and grade
active too), with `used` -- the customer's redemptions of it since its
`valid_from` -- and `redemptions`, their times (`YYYY-MM-DD HH:MM:SS`, company
time, oldest first). The device applies the usage window itself (today, last 7
days, month, period) and compares with `usage_limit`.

Only redeemed claims count: approved where approval is needed, and applied on a
closed bill.
"""


class CardUsageView(APIView):
    """POST /api/v1/{app}/card-discounts/usage -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Discount Cards"],
        summary="A customer's card discount usage",
        description=_USAGE_DESCRIPTION,
        request=envelope_request("CardUsageEnvelope", CardUsageRequest),
        responses=envelope_responses(
            (200, "ok", "Card usage.", _CARD_USAGE_SAMPLE),
            (400, "invalid_request", "full_number must be digits only.",
             {"errors": {"full_number": "must be digits only"}}),
            (404, "unknown_customer", "No customer found.", {}),
            (403, "customer_blocked", "The customer is blocked.", {}),
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
        form = CardUsageRequest(data=request_data)
        form.is_valid(raise_exception=True)

        try:
            customer, rows = services.card_usage(branch.company, form.validated_data["full_number"])
        except services.DiscountRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)

        return envelope("ok", "Card usage.", {
            "customer": {"customer_id": customer.pk, "customer_code": customer.customer_code,
                         "name": customer.full_name},
            "grades": [
                {
                    "card_type_id": discount.card_grade.card_type_id,
                    "card_type_name": discount.card_grade.card_type.name,
                    "card_grade_id": discount.card_grade_id, "card_grade_name": discount.card_grade.name,
                    "card_discount_id": discount.pk,
                    "usage_type": discount.usage_type, "usage_limit": discount.usage_limit,
                    "used": len(times), "redemptions": [t.strftime("%Y-%m-%d %H:%M:%S") for t in times],
                }
                for discount, times in rows
            ],
        })


# -- Approval request --------------------------------------------------------------

_APPROVAL_SAMPLE = {
    "sync_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6", "status": "pending",
    "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "card_discount_id": 12,
}

_APPROVAL_DESCRIPTION = """
Ask the web to approve an approval-mode card discount on one order. It shows on
the Card Discount Approval page as pending; ask its status later with the same
`sync_id`.

**sync_id** -- a **UUIDv7** made on the device for this request and resent
**unchanged** on retry. A resend is answered `duplicate` with the request's
current status and writes nothing.

`order_id` is the order's own `sync_id`; the order must be this company's and
have a customer. `full_number` is the card holder (country code then number,
digits only). The bill figures -- `bill_amount`, `discount_percent`,
`discount_amount`, `net_amount` -- are the device's own, stored as sent.
`card_photo` is the link to the photo the device uploaded. `requested_at` is
`YYYY-MM-DD HH:MM:SS`, company time.

One pending request per order at a time.
"""


class ApprovalRequestView(APIView):
    """POST /api/v1/{app}/card-discounts/approval -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Discount Cards"],
        summary="Ask for a card discount's approval",
        description=_APPROVAL_DESCRIPTION,
        request=envelope_request("ApprovalRequestEnvelope", ApprovalRequest),
        responses=envelope_responses(
            (200, "ok", "Approval requested.", _APPROVAL_SAMPLE),
            (200, "duplicate", "Already requested.", {**_APPROVAL_SAMPLE, "status": "approved"}),
            (400, "invalid_request", "sync_id must be a UUIDv7.", {"errors": {"sync_id": "must be a UUIDv7"}}),
            (400, "approval_not_needed", "This card discount needs no approval.", {}),
            (404, "unknown_order", "No order with that id.", {}),
            (404, "unknown_customer", "No customer found.", {}),
            (404, "unknown_card_discount", "No card discount with that id.", {}),
            (403, "customer_blocked", "The customer is blocked.", {}),
            (409, "request_pending", "This order already has a pending approval request.", {}),
            (409, "sync_id_conflict", "That sync_id is already used.", {}),
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
        form = ApprovalRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = dict(form.validated_data)
        values["requested_at"] = timezone.make_aware(values["requested_at"], zone_for(branch.company))

        try:
            claim, created = services.request_approval(request.auth, values)
        except services.DiscountRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)

        data = {"sync_id": str(claim.pk), "status": claim.status, "order_id": str(claim.order_id),
                "card_discount_id": claim.card_discount_id}
        if not created:
            return envelope("duplicate", "Already requested.", data)
        return envelope("ok", "Approval requested.", data)


# -- Approval status ---------------------------------------------------------------

_STATUS_SAMPLE = {
    "sync_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6", "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9",
    "status": "approved",
    "card_discount_id": 12, "card_type_name": "Corporate", "card_grade_name": "Gold",
    "bill_amount": "120.00", "discount_percent": "15.00", "discount_amount": "18.00", "net_amount": "102.00",
    "requested_at": "2026-09-30 17:05:00", "decided_at": "2026-09-30 17:09:42", "remarks": "Card checked",
}

_STATUS_DESCRIPTION = """
Where one approval request stands. `request_data`: the request's own `sync_id`
and the `order_id` it was made for -- both required, and they must agree.

* `pending` -- not decided yet; ask again later.
* `approved` -- bill with the discount (the figures are the ones sent).
* `rejected` -- bill without it; `remarks` may say why.
* `cancelled` -- the order was billed while the request was still pending.
* `redeemed` -- the discount was used on the closed bill.

`decided_at` is null while pending (for a cancelled request, when it was
cancelled); `remarks` is "" when none. Times `YYYY-MM-DD HH:MM:SS`, company time.
"""


def _amount(value):
    """Money: AED to the 3rd decimal."""
    return None if value is None else f"{value:.3f}"


def _percent(value):
    """A percentage: 2 decimals."""
    return None if value is None else f"{value:.2f}"


class ApprovalStatusView(APIView):
    """POST /api/v1/{app}/card-discounts/approval/status -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Discount Cards"],
        summary="Where an approval request stands",
        description=_STATUS_DESCRIPTION,
        request=envelope_request("ApprovalStatusEnvelope", ApprovalStatusRequest),
        responses=envelope_responses(
            (200, "ok", "Approval status.", _STATUS_SAMPLE),
            (400, "invalid_request", "sync_id is required.", {"errors": {"sync_id": "is required"}}),
            (404, "request_not_found", "No approval request found.", {}),
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
        form = ApprovalStatusRequest(data=request_data)
        form.is_valid(raise_exception=True)

        company = branch.company
        try:
            claim = services.approval_status(company, form.validated_data["order_id"],
                                             form.validated_data["sync_id"])
        except services.DiscountRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)

        zone = zone_for(company)
        return envelope("ok", "Approval status.", {
            "sync_id": str(claim.pk), "order_id": str(claim.order_id), "status": claim.status,
            "card_discount_id": claim.card_discount_id,
            "card_type_name": claim.card_type.name, "card_grade_name": claim.card_grade.name,
            "bill_amount": _amount(claim.bill_amount), "discount_percent": _percent(claim.discount_percent),
            "discount_amount": _amount(claim.discount_amount), "net_amount": _amount(claim.net_amount),
            "requested_at": claim.requested_at.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"),
            "decided_at": claim.decided_at.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
            if claim.decided_at else None,
            "remarks": claim.remarks,
        })
