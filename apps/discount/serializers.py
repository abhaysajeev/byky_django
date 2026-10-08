"""Type checks for the discount card device APIs' `request_data`."""

from decimal import Decimal

from rest_framework import serializers

from core.api import REQUIRED, datetime_field, text_field, uuid7_field, whole_number_field


def _full_number():
    """Country code then number, digits only -- the same string as
    Customer.mobile_full and the customer APIs' full_number."""
    return serializers.RegexField(
        r"^\d+$", max_length=30,
        error_messages={**REQUIRED, "invalid": "must be digits only",
                        "max_length": "must be at most 30 characters"},
    )


def _money(**kwargs):
    return serializers.DecimalField(
        max_digits=13, decimal_places=3, min_value=Decimal(0),
        error_messages={**REQUIRED, "invalid": "must be a number", "min_value": "must be 0 or more",
                        "max_decimal_places": "must have at most 3 decimal places"},
        **kwargs,
    )


class CardUsageRequest(serializers.Serializer):
    """The customer, by full number."""

    full_number = _full_number()


class CardDiscountsRequest(serializers.Serializer):
    """No fields -- the company is the session's own, and every card type,
    grade and discount it has is sent whole, as it is; the device picks."""


class ApprovalRequest(serializers.Serializer):
    """One approval-mode card discount on one order. The bill figures are the
    device's own and are stored as sent; the order and customer are looked up
    (apps/discount/services.py::request_approval)."""

    sync_id = uuid7_field()
    card_discount_id = whole_number_field(min_value=1)
    full_number = _full_number()
    order_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be an order's sync_id"})
    bill_amount = _money()
    discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0.01"), max_value=Decimal(100),
        error_messages={**REQUIRED, "invalid": "must be a number", "min_value": "must be more than 0",
                        "max_value": "must be 100 or less",
                        "max_decimal_places": "must have at most 2 decimal places"},
    )
    discount_amount = _money()
    net_amount = _money()
    requested_at = datetime_field()
    card_number = text_field(max_length=50, required=False, allow_blank=True)
    card_photo = text_field(max_length=500, required=False, allow_blank=True)


class ApprovalStatusRequest(serializers.Serializer):
    """One approval request, by its own sync_id and the order it is for --
    both must agree."""

    order_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be an order's sync_id"})
    sync_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})
