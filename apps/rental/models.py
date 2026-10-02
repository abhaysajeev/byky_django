"""Rental Management: the customer master.

Built from the client-approved wireframe (byky-main's
apps/byky_rms/templates/rms_customer_details_registration.html, Screen4_1 in
apps/byky_rms/views.py -- FSD 4.1, "row 57" redesign) and grounded against
the legacy DMSCustomer table (525,442 rows -- a live, heavily-used master;
analysis/_raw/columns.json).

Only the wireframe's own field set is carried forward. DMSCustomer also has
CustomerTypeID, CustomerCategoryID, LocationID/StateID, the Region/Cluster/
Block/UnitNo/Landmark sub-address columns, CurrentRating/RatingRemarks and
DiscountCard -- none of them appear in the client-approved redesign, so none
are modelled here. Not silently dropped: they're a real gap against the old
system for whoever designs FSD 4.1 properly, not invented back in from an
unrelated legacy table.

Blacklisting (FSD 4.3, a separate screen in the wireframe) is not built as
its own module yet -- is_blocked/block_reason are kept directly on Customer
instead (matching legacy DMSCustomer.IsBlock), so this one screen's CRUD is
self-contained; a dedicated sanction/penalty history screen is future work,
the same gap crew.EmployeeBlockLog fills for staff.
"""

import re

from django.db import models
from django.db.models import Case, F, Value, When

from core.models import ApprovalMixin, TimeStampedModel


def full_number(country_code, mobile_no):
    """The phone as one string of digits: country code, then number, with the
    `+` and any spaces or dashes dropped -- `+971`, `50 123-4567` ->
    `971501234567`. A customer's identity (Customer.mobile_full); the apps
    send the same string as `full_number`. One implementation for the model,
    the web form and the API."""
    return re.sub(r"\D", "", country_code or "") + re.sub(r"\D", "", mobile_no or "")


class Gender(models.TextChoices):
    MALE = "male", "Male"
    FEMALE = "female", "Female"
    OTHERS = "others", "Others"


class IdType(models.TextChoices):
    EMIRATES_ID = "emirates_id", "Emirates ID"
    PASSPORT = "passport", "Passport"
    DRIVING_LICENSE = "driving_license", "Driving License"
    OTHERS = "others", "Others"


class Customer(ApprovalMixin, TimeStampedModel):
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="customers")
    # Server-generated on create (services.next_customer_code) -- the
    # wireframe's own Add form has no Customer Code field, only its list
    # column does.
    customer_code = models.CharField("Customer Code", max_length=20)

    first_name = models.CharField("First Name", max_length=100)
    last_name = models.CharField("Last Name", max_length=100, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    # Text, not a master -- same call as Employee.nationality
    # (apps/crew/models.py): nobody has asked to filter or report by
    # nationality, and a lookup table nobody maintains goes stale.
    nationality = models.CharField(max_length=100, blank=True)

    id_type = models.CharField("ID Type", max_length=20, choices=IdType.choices, blank=True)
    # Not mandatory: a walk-in customer can be registered before their
    # document is captured.
    id_no = models.CharField("Document No", max_length=100, blank=True)
    # No drawer UI yet -- the shared drawer/CRUD plumbing has no working
    # file-upload path anywhere in the project (crew.Employee.photo is the
    # same kind of stub). Kept on the model for when that lands.
    id_document = models.FileField("Document Upload", upload_to="customers/", blank=True)

    # Dial code kept as free text, same reasoning as nationality -- a small
    # fixed list would need maintaining and the wireframe itself just types it.
    mobile_country_code = models.CharField("Country Code", max_length=10)
    mobile_no = models.CharField("Phone No", max_length=20)
    # Set by save() from the two fields above, never typed: the digits of
    # both, which is what makes a phone unique -- the same local number under
    # two country codes is two people.
    mobile_full = models.CharField("Full Number", max_length=30, editable=False)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    remarks = models.TextField(blank=True)

    # Distinct from is_active (ApprovalMixin): is_active is the record,
    # is_blocked is the person -- same split as crew.Employee.is_blocked.
    is_blocked = models.BooleanField(default=False)
    block_reason = models.CharField(max_length=255, blank=True)

    # The app's own UUIDv7 for a customer it created (customers/create), resent
    # unchanged on retry so an offline create is stored once. Sync checks
    # only -- the integer id stays the key. Empty for customers made on the
    # web; empty values never collide with each other.
    sync_id = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    class Meta:
        db_table = "customer"
        ordering = ["customer_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "customer_code"], name="uniq_customer_code_per_company",
                violation_error_message="A customer with this code already exists.",
            ),
            # Global, not per-company: one phone is one person whichever
            # company registered them. Its index is also what the device
            # lookup (apps/rental/api.py) finds a customer by.
            models.UniqueConstraint(
                fields=["mobile_full"], name="uniq_customer_mobile_full",
                violation_error_message="A customer with this phone number already exists.",
            ),
        ]
        indexes = [models.Index(fields=["company"])]

    def __str__(self):
        return self.full_name

    def save(self, *args, **kwargs):
        self.mobile_full = full_number(self.mobile_country_code, self.mobile_no)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and {"mobile_country_code", "mobile_no"} & set(update_fields):
            kwargs["update_fields"] = {*update_fields, "mobile_full"}
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return " ".join(part for part in (self.first_name, self.last_name) if part)


# --- Order ---------------------------------------------------------------------
#
# The design is order_lifecycle_design.md (section 4). Traced against the
# legacy DMS/RMS order tables (DMSOrder 164,019 rows, DMSOrderItems 266,310,
# DmsExitOrder, DMSReplacedOrders) -- "DMS" is FMCG lineage naming, but this is
# the vehicle-rental booking flow: DMSOrderItems.StockID points at
# ImsStockItem (vehicles), pricing at RmsFareDetails.
#
# Ids come from the tablet: Order.id and OrderItem.id are the UUIDv7 sync_ids
# the device makes offline, so it can return or replace a line before the
# server has ever replied. order_no is the tablet's receipt number -- unique
# per company, and also the invoice number.
#
# No bill before the ride ends (order_lifecycle_design.md 1 "Money"):
#   booking  -- a line's package and agreed base_fare; any advance paid;
#   return   -- that line's overtime_amount and total_amount;
#   settle   -- the order's bill: subtotal, discount, VAT, rounding, net_amount.
# Until then those fields are blank (NULL), never a guess.
#
# Money collected: amount_received / amount_refunded are running totals of the
# order's payment entries, written only by the payment service in the same
# transaction as the entry. paid_amount and balance_due follow from them in the
# database, so neither can drift from the payments (legacy's DMSOrder.PaidAmount
# vs SUM(DMSPayment.Amount)). payment_status follows `status`: pending while on
# rent, paid once settled -- settle needs the bill paid in full.


class OrderStatus(models.TextChoices):
    ACTIVE = "active", "Active"                    # legacy OrderStatusID=1 Running
    COMPLETED = "completed", "Completed"            # settled -- legacy =2 Received
    CANCELLED = "cancelled", "Cancelled"             # legacy =3


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"      # on rent: the bill is not made yet
    PAID = "paid", "Paid"               # settled, paid in full


class OrderItemStatus(models.TextChoices):
    ACTIVE = "active", "Active"        # legacy =1 Running
    RETURNED = "returned", "Returned"   # legacy =2 Received
    REPLACED = "replaced", "Replaced"    # swapped for another vehicle -- legacy =4
    REMOVED = "removed", "Removed"       # taken off the order, not billed -- legacy =3
    CANCELLED = "cancelled", "Cancelled"  # the whole order was cancelled


MONEY = {"max_digits": 12, "decimal_places": 2}

PAID = F("amount_received") - F("amount_refunded")


class Order(TimeStampedModel):
    id = models.UUIDField(primary_key=True, editable=False)

    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="orders")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="orders")
    device = models.ForeignKey("devices.Device", on_delete=models.PROTECT, related_name="orders")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    # The customer as at booking, beside the FK: the customer's own record can
    # change afterwards. customer_mobile is the full number (Customer.mobile_full).
    customer_name = models.CharField(max_length=200, blank=True)
    customer_mobile = models.CharField(max_length=20, blank=True)

    order_no = models.CharField(max_length=30)

    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.ACTIVE)
    is_direct_bill = models.BooleanField(default=False)
    is_hotel_order = models.BooleanField(default=False)
    hotel_commission = models.DecimalField(**MONEY, default=0)

    # Tablet times. created_on is when the server stored the booking.
    booked_at = models.DateTimeField()
    start_time = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # The bill -- blank until settle. net_amount = subtotal - discount_amount
    # + tax_amount + rounding_adjustment: what the customer pays.
    subtotal = models.DecimalField(**MONEY, null=True, blank=True)            # billed line totals
    # The one discount is a card discount, on the whole bill; discount_claim is
    # the claim applied (CardDiscountClaim.order lists every claim raised).
    discount_claim = models.OneToOneField(
        "discount.CardDiscountClaim", null=True, blank=True, on_delete=models.PROTECT,
        related_name="applied_to_order",
    )
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(**MONEY, null=True, blank=True)
    tax_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)   # VAT %
    tax_amount = models.DecimalField(**MONEY, null=True, blank=True)          # VAT on subtotal - discount
    rounding_adjustment = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(**MONEY, null=True, blank=True)

    # Money collected, from booking on.
    amount_received = models.DecimalField(**MONEY, default=0)
    amount_refunded = models.DecimalField(**MONEY, default=0)
    paid_amount = models.GeneratedField(
        expression=PAID, output_field=models.DecimalField(**MONEY), db_persist=True,
    )
    # Blank until settle (no bill yet); then 0 -- settle needs the bill paid.
    balance_due = models.GeneratedField(
        expression=F("net_amount") - PAID, output_field=models.DecimalField(**MONEY), db_persist=True,
    )
    payment_status = models.GeneratedField(
        expression=Case(
            When(status=OrderStatus.ACTIVE, then=Value(PaymentStatus.PENDING)),
            When(status=OrderStatus.COMPLETED, then=Value(PaymentStatus.PAID)),
            default=None,
        ),
        output_field=models.CharField(max_length=20, choices=PaymentStatus.choices, null=True),
        db_persist=True,
    )

    class Meta:
        db_table = "order"
        ordering = ["-booked_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "order_no"], name="uniq_order_no_per_company",
                violation_error_message="That order number is already used.",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    subtotal__gte=0, discount_amount__gte=0, net_amount__gte=0,
                    amount_received__gte=0, amount_refunded__gte=0,
                ),
                name="order_amounts_not_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["payment_status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["device"]),
            models.Index(fields=["booked_at"]),
        ]

    def __str__(self):
        return self.order_no


class OrderItem(TimeStampedModel):
    """One vehicle on an order. Replacing a vehicle closes this line as
    REPLACED and starts a new one pointing back at it (replaced_item); only
    the current line bills."""

    id = models.UUIDField(primary_key=True, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    vehicle = models.ForeignKey("fleet.Vehicle", on_delete=models.PROTECT, related_name="order_items")

    status = models.CharField(max_length=20, choices=OrderItemStatus.choices, default=OrderItemStatus.ACTIVE)

    # Traceability only -- the amounts below are what bills, and stay right if
    # the fare or offer is edited later.
    fare = models.ForeignKey("fare.Fare", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    offer = models.ForeignKey("fare.Offer", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    package_minutes = models.PositiveSmallIntegerField()

    start_time = models.DateTimeField()
    expected_end_time = models.DateTimeField()      # start + package
    end_time = models.DateTimeField(null=True, blank=True)   # returned / replaced / removed (tablet time)

    base_fare = models.DecimalField(**MONEY)        # the package price agreed at booking
    # Blank until the vehicle is returned. No discount and no VAT here: both
    # are on the order's bill.
    run_minutes = models.PositiveIntegerField(null=True, blank=True)     # how long it actually ran
    overtime_amount = models.DecimalField(**MONEY, null=True, blank=True)
    total_amount = models.DecimalField(**MONEY, null=True, blank=True)   # base_fare + overtime

    # The old line this one replaced -- legacy DMSReplacedOrders.
    replaced_item = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="replacement",
    )
    reason = models.TextField(blank=True)      # why it was replaced or removed

    class Meta:
        db_table = "order_item"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__isnull=True) | models.Q(end_time__gte=models.F("start_time")),
                name="order_item_end_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(expected_end_time__gte=models.F("start_time")),
                name="order_item_expected_end_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(base_fare__gte=0, overtime_amount__gte=0, total_amount__gte=0),
                name="order_item_amounts_not_negative",
            ),
            # A vehicle is on one active line at a time -- legacy trusted the
            # device's own state and had no such rule.
            models.UniqueConstraint(
                fields=["vehicle"], condition=models.Q(status=OrderItemStatus.ACTIVE),
                name="uniq_active_order_item_per_vehicle",
                violation_error_message="This vehicle is already on an active rental.",
            ),
        ]
        indexes = [models.Index(fields=["order"]), models.Index(fields=["vehicle", "status"])]

    def __str__(self):
        return f"{self.vehicle} on {self.order}"


# --- Payment ---------------------------------------------------------------------
#
# A payment entry, in the manner of ERPNext's Payment Entry: every movement of
# money is one row, and an order has any number of them -- part cash, part
# card (order_lifecycle_design.md 1, 4.4). The order carries no payment mode.
#
# amount is always positive; kind gives the direction: ADVANCE (booking or
# mid-rental) and SETTLEMENT (when the bill is settled) bring money in,
# REFUND sends it back.
# Entries are never edited or deleted -- a mistake is corrected by a reversing
# REFUND, so the trail stays whole. They are written only through
# apps.rental.services.record_payment, which keeps the order's
# amount_received / amount_refunded in step in the same transaction.
#
# Legacy DMSPayment hardcoded PaymentModeID = 1 on both of its insert paths
# (Save_Order_Booking, Service_Save_SubmitExit_Order): all 159,511 rows say
# Cash. mode here is what was actually used.


class PaymentKind(models.TextChoices):
    ADVANCE = "advance", "Advance"
    SETTLEMENT = "settlement", "Settlement"
    REFUND = "refund", "Refund"


MONEY_IN = (PaymentKind.ADVANCE, PaymentKind.SETTLEMENT)


class Payment(TimeStampedModel):
    id = models.UUIDField(primary_key=True, editable=False)      # the tablet's sync_id

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    kind = models.CharField(max_length=10, choices=PaymentKind.choices)
    mode = models.ForeignKey("company.PaymentMode", on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(**MONEY)

    # Card slip, cheque, gateway ref -- one pair for all of them; legacy kept
    # ChequeNo/ChequeDate and CardNumber/CardDate apart for the same fact.
    reference_no = models.CharField(max_length=50, blank=True)
    reference_date = models.DateField(null=True, blank=True)

    paid_at = models.DateTimeField()           # tablet time; created_on is when the server stored it
    device = models.ForeignKey(
        "devices.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="payments",
    )
    collected_by = models.ForeignKey(
        "core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
    )
    remarks = models.TextField(blank=True)

    class Meta:
        db_table = "payment"
        ordering = ["-paid_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_amount_positive"),
        ]
        indexes = [
            models.Index(fields=["order"]),
            models.Index(fields=["paid_at"]),
            models.Index(fields=["mode"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount} on {self.order}"


class OrderAction(models.TextChoices):
    BOOK = "book", "Booked"
    ADD = "add", "Vehicle added"
    REPLACE = "replace", "Vehicle replaced"
    REMOVE = "remove", "Vehicle removed"
    RETURN = "return", "Vehicle returned"
    PAYMENT = "payment", "Payment"
    SETTLE = "settle", "Settled"
    CANCEL_REQUEST = "cancel_request", "Cancel requested"
    CANCEL_APPROVED = "cancel_approved", "Cancel approved"
    CANCEL_REJECTED = "cancel_rejected", "Cancel rejected"


class OrderEvent(models.Model):
    """One call that changed an order -- its history, and what makes a resent
    call safe (order_lifecycle_design.md 2, 4.5).

    `id` is the call's own sync_id. A resend with the same body is answered
    with `response` again and writes nothing; the same id with a different
    body (`request_hash`) is a conflict. Append-only: never edited, so it
    carries no modified_* fields.
    """

    id = models.UUIDField(primary_key=True, editable=False)      # the call's sync_id

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="events")
    action = models.CharField(max_length=20, choices=OrderAction.choices)
    item = models.ForeignKey(
        OrderItem, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
    )
    new_item = models.ForeignKey(
        OrderItem, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
    )
    detail = models.JSONField(default=dict, blank=True)

    request_hash = models.CharField(max_length=64)               # SHA-256 of request_data
    response = models.JSONField(default=dict)                    # the reply's data, replayed as-is

    happened_at = models.DateTimeField()                         # tablet time
    received_at = models.DateTimeField(auto_now_add=True)        # server time
    device = models.ForeignKey(
        "devices.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="order_events",
    )
    user = models.ForeignKey("core.User", on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "order_event"
        ordering = ["happened_at"]
        indexes = [models.Index(fields=["order", "happened_at"])]

    def __str__(self):
        return f"{self.get_action_display()} on {self.order}"


# --- Invoice ------------------------------------------------------------------------
#
# Issued at settle, in the same transaction, and never edited: a frozen copy of
# the bill as it was (order_lifecycle_design.md 1 "Invoice", 4.7, 4.8). Legacy
# had no invoice table -- RMS_PRINT_INVOICE rebuilt one from the order at print
# time, so editing the order changed an invoice already issued.


class Invoice(TimeStampedModel):
    id = models.UUIDField(primary_key=True, editable=False)
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="invoice")
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="invoices")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="invoices")
    device = models.ForeignKey("devices.Device", on_delete=models.PROTECT, related_name="invoices")  # settling tablet
    issued_by = models.ForeignKey("core.User", on_delete=models.PROTECT, related_name="+")

    invoice_no = models.CharField(max_length=30)               # = the order's order_no
    issued_at = models.DateTimeField()                          # = settle time (tablet)

    # As at issue.
    company_name = models.CharField(max_length=200)
    company_trn = models.CharField(max_length=100, blank=True)
    branch_name = models.CharField(max_length=200)
    customer_name = models.CharField(max_length=200, blank=True)
    customer_mobile = models.CharField(max_length=20, blank=True)

    subtotal = models.DecimalField(**MONEY)
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(**MONEY)
    tax_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    tax_amount = models.DecimalField(**MONEY)
    rounding_adjustment = models.DecimalField(max_digits=6, decimal_places=2)
    net_amount = models.DecimalField(**MONEY)
    payments = models.JSONField(default=list)      # [{mode, kind, amount, reference_no}] at issue

    class Meta:
        db_table = "invoice"
        ordering = ["-issued_at"]
        constraints = [
            models.UniqueConstraint(fields=["company", "invoice_no"], name="uniq_invoice_no_per_company"),
        ]
        indexes = [models.Index(fields=["branch", "issued_at"])]

    def __str__(self):
        return self.invoice_no


class InvoiceItem(models.Model):
    """One billed vehicle line, as at issue. Replaced and removed lines are not billed."""

    id = models.UUIDField(primary_key=True, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name="+")

    vehicle_identifier = models.CharField(max_length=20)
    vehicle_name = models.CharField(max_length=200)
    vehicle_type = models.CharField(max_length=200)
    package_minutes = models.PositiveSmallIntegerField()
    run_minutes = models.PositiveIntegerField(null=True, blank=True)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    base_fare = models.DecimalField(**MONEY)
    overtime_amount = models.DecimalField(**MONEY)
    total_amount = models.DecimalField(**MONEY)

    class Meta:
        db_table = "invoice_item"
        ordering = ["start_time"]

    def __str__(self):
        return f"{self.vehicle_name} on {self.invoice}"
