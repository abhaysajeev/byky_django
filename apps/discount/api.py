"""The discount card device APIs -- views only translate HTTP.

POST /api/v1/{app}/card-discounts: every card type of the device's company,
the grades under each, and each grade's discount configuration -- sent as it
is (active or not, any validity); the device picks what applies.

POST /api/v1/{app}/card-discounts/usage: a customer's (by full number)
redemptions of each discount usable today; the device applies the window.
"""

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company.models import WeekDay
from apps.discount import services
from apps.discount.models import CardDiscount, CardGrade, CardType
from apps.discount.serializers import CardDiscountsRequest, CardUsageRequest
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, request_parts, session_station
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses


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
        except services.CardUsageRefused as refusal:
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
