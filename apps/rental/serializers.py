"""Type checks for the rental APIs' `request_data`. Same pattern as
apps/fare/serializers.py and apps/fleet/serializers.py -- types only; the
lookup, the customer create and the order calls themselves are
apps/rental/services.py (customer_by_phone, create_customer,
create_rental_order, order_at_station)."""

from decimal import Decimal

from rest_framework import serializers

from apps.rental.models import (
    DiscountType,
    Gender,
    IdType,
    OrderRequestKind,
    PaymentKind,
    full_number,
)
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
    """An AED amount, to the 3rd decimal (7 Oct 2026)."""
    return serializers.DecimalField(
        max_digits=13, decimal_places=3, min_value=min_value,
        error_messages={**REQUIRED, "invalid": "must be a number", "min_value": f"must be {min_value} or more",
                        "max_decimal_places": "must have at most 3 decimal places"},
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


def _existing_id():
    """An order or item the tablet already holds -- any UUID (orders from before
    the tablet-made ids are not v7)."""
    return serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})


class OrderReturnRequest(serializers.Serializer):
    """One vehicle back. `sync_id` is this call's own id. The tablet works out
    the run time and the amounts; total_amount must be the line's base fare +
    overtime_amount."""

    sync_id = uuid7_field()
    order_id = _existing_id()
    item_id = _existing_id()
    returned_at = datetime_field()
    run_minutes = whole_number_field()
    overtime_amount = _money_field()
    total_amount = _money_field()


class OrderAddRequest(serializers.Serializer):
    """Another vehicle joins an active order: one new line (its own sync_id,
    package and base fare), and any advance taken for it -- recorded in this
    same call."""

    sync_id = uuid7_field()
    order_id = _existing_id()
    added_at = datetime_field()
    item = OrderItemRequest()
    payments = OrderPaymentRequest(many=True, required=False)

    def validate_payments(self, payments):
        if _repeated(payment["sync_id"] for payment in payments):
            raise serializers.ValidationError("Each payment needs its own sync_id.")
        return payments


class OrderReplaceRequest(serializers.Serializer):
    """A vehicle swapped for another: the old line closes as replaced (with
    the reason, never billed), the new line starts."""

    sync_id = uuid7_field()
    order_id = _existing_id()
    old_item_id = _existing_id()
    replaced_at = datetime_field()
    reason = text_field(max_length=500)
    new_item = OrderItemRequest()


class OrderRemoveRequest(serializers.Serializer):
    """A vehicle taken off the order, no replacement: the line stays as
    removed, with the reason, never billed."""

    sync_id = uuid7_field()
    order_id = _existing_id()
    item_id = _existing_id()
    removed_at = datetime_field()
    reason = text_field(max_length=500)


class StandalonePaymentRequest(serializers.Serializer):
    """One entry of a standalone payments call -- its kind is the call's."""

    sync_id = uuid7_field()
    payment_mode_id = whole_number_field()
    amount = _money_field(min_value=Decimal("0.01"))
    reference_no = text_field(max_length=50, required=False, allow_blank=True)
    reference_date = date_field(required=False, allow_null=True)
    paid_at = datetime_field()


class OrderPaymentsRequest(serializers.Serializer):
    """Money moving on its own: an extra advance, or a refund. One `kind` for
    the whole call, so advances and refunds are never mixed in one."""

    sync_id = uuid7_field()
    order_id = _existing_id()
    kind = serializers.ChoiceField(
        choices=[PaymentKind.ADVANCE, PaymentKind.REFUND],
        error_messages={**REQUIRED, "invalid_choice": "must be advance or refund"},
    )
    payments = StandalonePaymentRequest(many=True)

    def validate_payments(self, payments):
        if not payments:
            raise serializers.ValidationError("Send at least one payment.")
        if _repeated(payment["sync_id"] for payment in payments):
            raise serializers.ValidationError("Each payment needs its own sync_id.")
        return payments


class SettlePaymentRequest(OrderPaymentRequest):
    """A payment taken at settle: the balance (`settlement`), or money handed
    back when the customer paid more than the bill (`refund`)."""

    kind = serializers.ChoiceField(
        choices=[PaymentKind.SETTLEMENT, PaymentKind.REFUND],
        error_messages={**REQUIRED, "invalid_choice": "must be settlement or refund"},
    )


def _percent_field(**kwargs):
    return serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0.01"), max_value=Decimal("100"),
        error_messages={**REQUIRED, "invalid": "must be a number", "min_value": "must be more than 0",
                        "max_value": "must be 100 or less",
                        "max_decimal_places": "must have at most 2 decimal places"},
        **kwargs,
    )


class SettleCardDiscountRequest(serializers.Serializer):
    """The card discount applied: an approved card-discount request
    (`request_sync_id` -- the sync_id sent to card-discounts/approval), or an
    automatic discount (`card_discount_id`, with the card's number) -- one of
    the two."""

    request_sync_id = serializers.UUIDField(required=False, allow_null=True,
                                            error_messages={"invalid": "must be a UUID"})
    card_discount_id = whole_number_field(required=False, allow_null=True)
    card_number = text_field(max_length=50, required=False, allow_blank=True)
    discount_percentage = _percent_field()
    discount_amount = _money_field()

    def validate(self, values):
        if bool(values.get("request_sync_id")) == bool(values.get("card_discount_id")):
            raise serializers.ValidationError({"request_sync_id": "or card_discount_id is required, not both"})
        return values


class SettleComplimentaryRequest(serializers.Serializer):
    """An approved complimentary ride (`request_sync_id` -- the sync_id sent to
    orders/requests). The whole subtotal is the discount: net 0, VAT 0."""

    request_sync_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})


class SettleManagerDiscountRequest(serializers.Serializer):
    """A manager's approved discount (`request_sync_id` -- the sync_id sent to
    orders/requests). For a percentage approval send the same
    `discount_percentage`; for an AED approval leave it out."""

    request_sync_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})
    discount_percentage = _percent_field(required=False, allow_null=True)
    discount_amount = _money_field()


class OrderSettleRequest(serializers.Serializer):
    """Close the bill. The tablet works out every amount; the server checks
    only that the subtotal is the returned vehicles' total, the net adds up
    (subtotal - discount + rounding: fares include VAT), and the bill is paid
    in full after `payments`. tax_percentage / tax_amount are the VAT included
    in the net, for the tax invoice."""

    sync_id = uuid7_field()
    order_id = _existing_id()
    settled_at = datetime_field()
    subtotal = _money_field()
    card_discount = SettleCardDiscountRequest(required=False, allow_null=True)
    manager_discount = SettleManagerDiscountRequest(required=False, allow_null=True)
    complimentary = SettleComplimentaryRequest(required=False, allow_null=True)
    tax_percentage = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal(0),
        error_messages={**REQUIRED, "invalid": "must be a number", "min_value": "must be 0 or more"},
    )
    tax_amount = _money_field()
    rounding_adjustment = serializers.DecimalField(
        max_digits=7, decimal_places=3,
        error_messages={**REQUIRED, "invalid": "must be a number",
                        "max_decimal_places": "must have at most 3 decimal places"},
    )
    net_amount = _money_field()
    payments = SettlePaymentRequest(many=True, required=False)

    def validate_payments(self, payments):
        if _repeated(payment["sync_id"] for payment in payments):
            raise serializers.ValidationError("Each payment needs its own sync_id.")
        return payments

    def validate(self, values):
        sent = [key for key in ("card_discount", "manager_discount", "complimentary") if values.get(key)]
        if len(sent) > 1:
            raise serializers.ValidationError(
                {sent[-1]: "send one: card_discount, manager_discount or complimentary"})
        return values


class CreditNoteRequest(serializers.Serializer):
    """A tablet asks for a credit note on a settled order. `sync_id` is this
    call's id and becomes the credit note's id; the amount is set on the web."""

    sync_id = uuid7_field()
    order_id = _existing_id()
    requested_at = datetime_field()
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True,
                                   error_messages={"max_length": "must be at most 500 characters"})


class CreditNoteCancelRequest(serializers.Serializer):
    """A tablet withdraws its request while it is still waiting."""

    sync_id = uuid7_field()
    credit_note_id = _existing_id()
    cancelled_at = datetime_field()


class CreditNoteStatusRequest(serializers.Serializer):
    """Every credit note on these orders -- requested on a tablet or issued on
    the web."""

    order_ids = serializers.ListField(
        child=serializers.UUIDField(error_messages={"invalid": "must be an order's sync_id"}),
        min_length=1, max_length=100,
        error_messages={**REQUIRED, "min_length": "send at least one order_id",
                        "max_length": "send at most 100 order_ids", "not_a_list": "must be a list"},
    )


# -- Requests (apps/rental/requests.py) --------------------------------------------


class OrderRequestCreateRequest(serializers.Serializer):
    """A tablet asks a manager about a running order. `sync_id` is this call's
    id and becomes the request's id."""

    sync_id = uuid7_field()
    order_id = _existing_id()
    kind = serializers.ChoiceField(
        choices=OrderRequestKind.choices,
        error_messages={**REQUIRED, "invalid_choice": "must be one of: discount, complimentary, reprint"},
    )
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True,
                                   error_messages={"max_length": "must be at most 500 characters"})
    requested_at = datetime_field()


class OrderRequestWithdrawRequest(serializers.Serializer):
    """A tablet takes back a request still waiting. `sync_id` is this call's
    own id; `request_sync_id` the request's."""

    sync_id = uuid7_field()
    request_sync_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})
    withdrawn_at = datetime_field()


class OrderRequestUsedRequest(serializers.Serializer):
    """The tablet printed the bill under an approved reprint. `sync_id` is this
    call's own id; `request_sync_id` the reprint request's."""

    sync_id = uuid7_field()
    request_sync_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})
    used_at = datetime_field()


class OrderRequestStatusRequest(CreditNoteStatusRequest):
    """Every request on these orders."""


class ManagerRequestListRequest(serializers.Serializer):
    pending = serializers.BooleanField(required=False, default=False,
                                       error_messages={"invalid": "must be true or false"})
    kind = serializers.ChoiceField(
        choices=OrderRequestKind.choices, required=False, allow_blank=True,
        error_messages={"invalid_choice": "must be one of: discount, complimentary, reprint"},
    )
    branch_id = whole_number_field(min_value=1, required=False, allow_null=True)
    page = whole_number_field(min_value=1, required=False, default=1)


class ManagerRequestDecisionRequest(serializers.Serializer):
    """Reject or revoke: the request and an optional note."""

    request_sync_id = serializers.UUIDField(error_messages={**REQUIRED, "invalid": "must be a UUID"})
    note = serializers.CharField(max_length=500, required=False, allow_blank=True,
                                 error_messages={"max_length": "must be at most 500 characters"})


class ManagerRequestApproveRequest(ManagerRequestDecisionRequest):
    """Approve a request. A discount takes its rule: `percent` (up to 100, 2
    decimals) or `amount` (AED, 3 decimals) -- the bill is not made yet. A
    complimentary or a reprint takes none."""

    discount_type = serializers.ChoiceField(
        choices=DiscountType.choices, required=False, allow_null=True,
        error_messages={"invalid_choice": "must be percent or amount"},
    )
    discount_value = serializers.DecimalField(
        max_digits=13, decimal_places=3, min_value=Decimal("0.001"), required=False, allow_null=True,
        error_messages={**REQUIRED, "invalid": "must be a number", "min_value": "must be more than 0",
                        "max_decimal_places": "must have at most 3 decimal places"},
    )
