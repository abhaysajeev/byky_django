"""Type checks for the rental APIs' `request_data`. Same pattern as
apps/fare/serializers.py and apps/fleet/serializers.py -- types only; the
lookup, the customer create and the order calls themselves are
apps/rental/services.py (customer_by_phone, create_customer,
create_rental_order, order_at_station)."""

from decimal import Decimal

from rest_framework import serializers

from apps.rental.models import Gender, IdType, PaymentKind, full_number
from core.api import (
    REQUIRED,
    date_field,
    datetime_field,
    text_field,
    uuid7_field,
    whole_number_field,
)


class PhoneFields(serializers.Serializer):
    """The phone, three ways, all required: the app sends the full number it
    built so the server can match on it directly, and the two parts so the
    customer's record keeps them -- and the three must agree."""

    mobile_country_code = text_field(max_length=10)
    mobile_no = text_field(max_length=20)
    full_number = serializers.RegexField(
        r"^\d+$", max_length=30,
        error_messages={**REQUIRED, "invalid": "must be digits only",
                        "max_length": "must be at most 30 characters"},
    )

    def validate(self, values):
        if values["full_number"] != full_number(values["mobile_country_code"], values["mobile_no"]):
            raise serializers.ValidationError(
                {"full_number": "does not match mobile_country_code and mobile_no"}
            )
        return values


class CustomerLookupRequest(PhoneFields):
    """Matched on full_number."""


def _optional_text(max_length=None):
    if max_length is None:
        return serializers.CharField(required=False, allow_blank=True,
                                     error_messages={"invalid": "must be text"})
    return text_field(max_length=max_length, required=False, allow_blank=True)


def _choice(choices, names):
    return serializers.ChoiceField(
        choices=choices, required=False, allow_blank=True,
        error_messages={"invalid_choice": f"must be one of {names}"},
    )


class CustomerCreateRequest(PhoneFields):
    """A new customer from the operator app. Only first_name and the phone
    are required -- a walk-in is registered before their document is seen."""

    sync_id = uuid7_field()
    first_name = text_field(max_length=100)
    last_name = _optional_text(100)
    gender = _choice(Gender.choices, ", ".join(Gender.values))
    date_of_birth = date_field(required=False, allow_null=True)
    nationality = _optional_text(100)
    id_type = _choice(IdType.choices, ", ".join(IdType.values))
    id_no = _optional_text(100)
    email = serializers.EmailField(required=False, allow_blank=True,
                                   error_messages={"invalid": "must be a valid email"})
    address = _optional_text()
    remarks = _optional_text()


def _money_field(min_value=Decimal(0), **kwargs):
    return serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=min_value,
        error_messages={**REQUIRED, "invalid": "must be a number", "min_value": f"must be {min_value} or more"},
        **kwargs,
    )


class OrderItemRequest(serializers.Serializer):
    """One vehicle line. `sync_id` is the line's own id, made on the tablet so
    it can return or replace the line before the server has replied
    (order_lifecycle_design.md 2). vehicle_id, fare_id and offer_id are ids
    the device already holds from /vehicles and /fares -- never a code to
    resolve here."""

    sync_id = uuid7_field()
    vehicle_id = whole_number_field()
    fare_id = whole_number_field(required=False, allow_null=True)
    offer_id = whole_number_field(required=False, allow_null=True)
    package_minutes = whole_number_field(min_value=1)
    start_time = datetime_field()
    expected_end_time = datetime_field()
    # The package price agreed now. Everything else on the bill comes later:
    # the line's final amount at return, the order's bill at settle.
    base_fare = _money_field()

    def validate(self, values):
        if values["expected_end_time"] < values["start_time"]:
            raise serializers.ValidationError({"expected_end_time": "must not be before start_time"})
        return values


class OrderPaymentRequest(serializers.Serializer):
    """One payment entry (design 4.4). At booking only an `advance`; the rest
    of the kinds arrive with their own calls (settle, payments)."""

    sync_id = uuid7_field()
    kind = serializers.ChoiceField(
        choices=[PaymentKind.ADVANCE], error_messages={**REQUIRED, "invalid_choice": "must be advance"},
    )
    payment_mode_id = whole_number_field()
    amount = _money_field(min_value=Decimal("0.01"))
    reference_no = text_field(max_length=50, required=False, allow_blank=True)
    reference_date = date_field(required=False, allow_null=True)
    paid_at = datetime_field()


class OrderCreateRequest(serializers.Serializer):
    """One rental booking, whole -- the order, its vehicle lines and any
    advance payments, in one call. No bill yet: each line carries its agreed
    base fare, and the bill is made at settle (design 1 "Money").

    `sync_id` is the order's id and this call's, a UUIDv7 made on the tablet
    and resent unchanged on retry. The branch, device and user are never
    sent -- they are the signed-in session's own.
    """

    sync_id = uuid7_field()
    order_no = text_field(max_length=30)                # the receipt number, unique per company
    customer_id = whole_number_field()

    # booked_at is when the booking happened on the tablet, possibly offline,
    # well before this call reaches the server; start_time is the rental's own.
    booked_at = datetime_field()
    start_time = datetime_field()

    is_direct_bill = serializers.BooleanField(required=False, default=False)
    is_hotel_order = serializers.BooleanField(required=False, default=False)
    hotel_commission = _money_field(required=False)

    items = OrderItemRequest(many=True)
    payments = OrderPaymentRequest(many=True, required=False)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("An order needs at least one vehicle.")
        if _repeated(item["sync_id"] for item in items):
            raise serializers.ValidationError("Each item needs its own sync_id.")
        return items

    def validate_payments(self, payments):
        if _repeated(payment["sync_id"] for payment in payments):
            raise serializers.ValidationError("Each payment needs its own sync_id.")
        return payments


def _repeated(values):
    values = list(values)
    return len(set(values)) != len(values)


class OrderDetailRequest(serializers.Serializer):
    """One order, by its sync_id or its order number -- exactly one."""

    sync_id = serializers.UUIDField(required=False, allow_null=True,
                                    error_messages={"invalid": "must be a UUID"})
    order_no = text_field(max_length=30, required=False, allow_blank=True)

    def validate(self, values):
        if values.get("sync_id") and values.get("order_no"):
            raise serializers.ValidationError({"order_no": "must not be sent with sync_id"})
        if not values.get("sync_id") and not values.get("order_no"):
            raise serializers.ValidationError({"sync_id": "or order_no is required"})
        return values
