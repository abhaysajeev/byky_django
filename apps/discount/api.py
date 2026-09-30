"""The discount card device APIs -- views only translate HTTP.

POST /api/v1/{app}/card-discounts: every card type of the device's company,
the grades under each, and each grade's discount configuration -- sent as it
is (active or not, any validity); the device picks what applies.
"""

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company.models import WeekDay
from apps.discount.models import CardDiscount, CardGrade, CardType
from apps.discount.serializers import CardDiscountsRequest
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
