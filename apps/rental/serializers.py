"""Type checks for the rental order APIs' `request_data`. Same pattern as
apps/fare/serializers.py and apps/fleet/serializers.py -- types only; the
lookup and create rules are apps/rental/services.py::customer_by_phone,
create_rental_order."""

from decimal import Decimal

from rest_framework import serializers

from core.api import REQUIRED, datetime_field, text_field, uuid7_field, whole_number_field


class CustomerLookupRequest(serializers.Serializer):
    """mobile_no only -- the local number, no country code. The app sends
    what a station operator types or scans; mobile_country_code is not part
    of the match."""

    mobile_no = text_field(max_length=20)


def _money_field(**kwargs):
    return serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal(0),
        error_messages={**REQUIRED, "invalid": "must be a number", "min_value": "must be 0 or more"},
        **kwargs,
    )


class OrderItemRequest(serializers.Serializer):
    """One vehicle line. vehicle_id, fare_id and offer_id are ids the device
    already holds from /vehicles, /fares and (once it exists) an offers
    download -- never a code to resolve here, the same as every other
    lookup-by-download field in this project."""

    vehicle_id = whole_number_field()
    fare_id = whole_number_field(required=False, allow_null=True)
    offer_id = whole_number_field(required=False, allow_null=True)
    package_minutes = whole_number_field(min_value=1)
    start_time = datetime_field()
    expected_end_time = datetime_field()
    rate = _money_field()
    amount = _money_field()
    discount = _money_field(required=False)
    tax_amount = _money_field(required=False)
    total_amount = _money_field()
    remarks = text_field(max_length=500, required=False, allow_blank=True)


class OrderCreateRequest(serializers.Serializer):
    """One rental booking, whole -- order header, its vehicle lines, and (if
    money was collected) the first payment, in one call. Device money
    figures (total/tax/net/rate/amount/...) are trusted as sent, not
    recomputed server-side against apps.fare.pricing (design decision).

    `sync_id` is a UUIDv7 made on the device and resent unchanged on retry;
    it becomes the order's id. A resend answers `duplicate` and writes
    nothing new, the same contract as apps.crew.api's attendance mark.

    The branch, device and creating user are never sent -- they are the
    signed-in session's own (session_station / request.auth), the same rule
    apps/devices/api.py::RegistrationView documents for a branch: never
    revealed or accepted before or outside the session that owns it.
    """

    sync_id = uuid7_field()
    order_no = text_field(max_length=30, required=False, allow_blank=True)
    customer_id = whole_number_field()

    # device_created_at is when the booking happened on the device, possibly
    # offline, well before this call reaches the server; start_time is the
    # rental's own start, which can differ (a scheduled booking).
    device_created_at = datetime_field()
    start_time = datetime_field()

    payment_mode_id = whole_number_field()
    is_direct_bill = serializers.BooleanField(required=False, default=False)
    collected_amount = _money_field(required=False)          # first Payment, if any

    total_amount = _money_field()
    total_discount = _money_field(required=False)
    total_tax = _money_field(required=False)
    tax_percentage = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal(0), required=False,
        error_messages={**REQUIRED, "invalid": "must be a number"},
    )
    rounded_diff = serializers.DecimalField(
        max_digits=6, decimal_places=2, required=False,
        error_messages={**REQUIRED, "invalid": "must be a number"},
    )
    net_amount = _money_field()
    advance_amount = _money_field(required=False)

    is_hotel_order = serializers.BooleanField(required=False, default=False)
    hotel_commission = _money_field(required=False)

    items = OrderItemRequest(many=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("An order needs at least one vehicle.")
        return items
