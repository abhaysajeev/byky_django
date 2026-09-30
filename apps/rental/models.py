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

from django.db import models

from core.ids import uuid7
from core.models import ApprovalMixin, TimeStampedModel


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
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    remarks = models.TextField(blank=True)

    # Distinct from is_active (ApprovalMixin): is_active is the record,
    # is_blocked is the person -- same split as crew.Employee.is_blocked.
    is_blocked = models.BooleanField(default=False)
    block_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "customer"
        ordering = ["customer_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "customer_code"], name="uniq_customer_code_per_company",
                violation_error_message="A customer with this code already exists.",
            ),
            # Global, not per-company: the device lookup API
            # (apps/rental/api.py) matches on this alone, and one phone
            # number is one person regardless of which company registered
            # them.
            models.UniqueConstraint(
                fields=["mobile_no"], name="uniq_customer_mobile_no",
                violation_error_message="A customer with this phone number already exists.",
            ),
        ]
        indexes = [models.Index(fields=["company"]), models.Index(fields=["mobile_no"])]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self):
        return " ".join(part for part in (self.first_name, self.last_name) if part)


# --- Order ---------------------------------------------------------------------
#
# Traced against the legacy DMS/RMS order tables (DMSOrder 164,019 rows,
# DMSOrderItems 266,310, DmsExitOrder, DMSReplacedOrders) -- "DMS" is FMCG
# lineage naming, but this is the vehicle-rental booking flow: DMSOrderItems
# .StockID points at ImsStockItem (vehicles), pricing at RmsFareDetails.
#
# id is the device's sync_id -- a UUIDv7 made offline and resent unchanged on
# retry, same pattern as crew.models.Attendance. It is the order's real
# identity: the whole Order + OrderItem graph is built on the device before
# any network round trip, so items can reference their parent immediately.
# order_no is a second, separate field -- whatever string the device sends,
# unvalidated and unenforced server-side (legacy's own OrderNo uniqueness
# check was a workaround for having no better idempotency key; sync_id
# replaces the need for it, see design discussion).
#
# Money: legacy's live rental data (DMSOrder.DepositAmount) has no separate
# "advance" concept -- SfaOrder.AdvanceAmount exists only on the dead 0-row
# SFA/FMCG family. The device API's advance is simply an early rental
# payment, not a refundable security hold: Order.advance_amount is what was
# asked for at booking (a snapshot of intent), and every payment -- whether
# collected as the advance or later -- counts toward the same paid_amount
# against net_amount. See Payment/PaymentKind below.
#
# Deliberately not modelled yet, on a separate migration once Order exists to
# FK against:
#   - CreditNoteRequest   legacy DMSCreditNoteRequest hard-deletes a rejected
#                         request (after copying it to a history table) --
#                         modelled as its own child table with a status field
#                         instead, so nothing is ever lost.
#   - card_discount / membership-card promotions -- legacy's
#     Service_Save_RequestApproval_CardDiscount is a whole card + SMS-OTP +
#     expiry subsystem (DMSCard, DMSCardPhone, RMSCardDiscount, the generic
#     DMSRequests table), not a flat discount field. Not traced enough to
#     model yet -- a real gap, not silently dropped.
#   - Offer pricing/precedence -- OrderItem.offer below links to the scheme
#     used, but apps.fare.pricing has no offer support yet; the FK is a
#     placeholder for when it does.


class OrderStatus(models.TextChoices):
    ACTIVE = "active", "Active"                    # legacy OrderStatusID=1 Running
    PAYMENT_PENDING = "payment_pending", "Payment Pending"   # legacy =5 Processing
    COMPLETED = "completed", "Completed"            # every item returned
    CANCELLED = "cancelled", "Cancelled"             # legacy =3


class OrderItemStatus(models.TextChoices):
    ACTIVE = "active", "Active"        # legacy =1 Running
    RETURNED = "returned", "Returned"   # legacy =2 Received
    CANCELLED = "cancelled", "Cancelled"  # legacy =3
    REPLACED = "replaced", "Replaced"    # legacy =4


class Order(TimeStampedModel):
    id = models.UUIDField(primary_key=True, editable=False)

    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="orders")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="orders")
    device = models.ForeignKey("devices.Device", on_delete=models.PROTECT, related_name="orders")
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.PROTECT, related_name="orders")
    # What the customer looked like at booking time, beside the FK -- same
    # reasoning as Attendance.employee_code/employee_name: the customer's
    # own record can change (name corrected, re-blocked) after the fact, and
    # this is what was true when the order was placed.
    customer_name = models.CharField(max_length=200, blank=True)
    customer_mobile = models.CharField(max_length=20, blank=True)

    # Whatever the device sends -- not validated or enforced unique here.
    order_no = models.CharField(max_length=30, blank=True)
    # Server-assigned, kept for later use -- not generated yet (see design
    # discussion: distinct from order_no, the payload's InvoiceNumber implies
    # a real persisted invoice identity this system doesn't derive live the
    # way legacy's RMS_PRINT_INVOICE does).
    invoice_no = models.CharField(max_length=30, blank=True)

    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.ACTIVE)

    device_created_at = models.DateTimeField()
    synced_at = models.DateTimeField(null=True, blank=True)
    start_time = models.DateTimeField()
    number_of_vehicles = models.PositiveSmallIntegerField()

    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total_discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    rounded_diff = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2)
    # What was asked for at booking -- a snapshot of intent, not a running
    # balance. Renamed from deposit_amount: legacy's live rental data has no
    # separate refundable-security concept (see the module docstring above),
    # so this is just the advance portion of the rental payment.
    advance_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_mode = models.ForeignKey("company.PaymentMode", on_delete=models.PROTECT, related_name="orders")
    is_direct_bill = models.BooleanField(default=False)

    # Cache, not the source of truth -- sum of this order's ADVANCE and
    # RENTAL payments, less any REFUND (Payment/PaymentKind below). Left
    # plain (not a derived property) so it stays cheap to query/list without
    # a join, the same trade Vehicle.identifier and
    # Device.device_registration_id make.
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    is_hotel_order = models.BooleanField(default=False)
    hotel_commission = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        db_table = "order"
        ordering = ["-device_created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_amount__gte=0, net_amount__gte=0, paid_amount__gte=0),
                name="order_amounts_not_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["device"]),
        ]

    def __str__(self):
        return self.order_no or str(self.id)

    @property
    def is_paid(self):
        return self.paid_amount >= self.net_amount


class OrderItem(models.Model):
    # Server-generated, unlike Order's device-sent sync_id -- items arrive
    # nested inside the order-create call, so the order's own id is the only
    # identity the device needs to supply; a retry is already deduplicated at
    # that level.
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    vehicle = models.ForeignKey("fleet.Vehicle", on_delete=models.PROTECT, related_name="order_items")

    status = models.CharField(max_length=20, choices=OrderItemStatus.choices, default=OrderItemStatus.ACTIVE)

    start_time = models.DateTimeField()
    # Scheduled, sent by the device at creation (start_time + package) --
    # distinct from end_time, which is the actual return time, filled later.
    # Lets the app flag an overdue rental before anyone touches this row.
    expected_end_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)

    # Traceability only -- the numbers below are the snapshot that actually
    # bills, and stay correct even if the fare rule is edited or deactivated
    # later. Nullable: a fare can be deleted (PROTECT stops that in practice,
    # but the column allows for a future on_delete change without a data
    # migration).
    fare = models.ForeignKey("fare.Fare", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    # Placeholder link only -- apps.fare.pricing has no offer/scheme support
    # yet, so nothing here reads or applies this FK. Kept so the device's
    # SchemeID has somewhere to land without inventing pricing logic early.
    offer = models.ForeignKey("fare.Offer", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    package_minutes = models.PositiveSmallIntegerField()

    rate = models.DecimalField(max_digits=10, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)

    # Points at the OLD line this one supersedes, set by the replace flow
    # (legacy Service_Save_Replace / DMSReplacedOrders). The old line's own
    # status moves to REPLACED; nothing here points forward from old to new,
    # only backward from new to old -- same direction as
    # Device.replaced_device.
    replaced_item = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="replacement",
    )
    remarks = models.TextField(blank=True)

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
                condition=models.Q(rate__gte=0, amount__gte=0, total_amount__gte=0),
                name="order_item_amounts_not_negative",
            ),
            # A vehicle can only be on one active rental line at a time --
            # legacy had no such rule at the database level (Save_Order_Booking
            # trusts the device's own state), this closes that gap.
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
# One kind, one running total: an advance collected at booking and a rental
# payment collected later both count toward the same Order.paid_amount --
# legacy's DepositAmount/AdvanceAmount split doesn't survive in the live
# rental data (see the Order docstring above), so there is no separate
# refundable-hold balance to track. REFUND exists for the one real "money
# goes back out" case: the advance collected turns out to be more than the
# final bill.
#
# mode is a real FK to company.PaymentMode, not a second hardcoded enum --
# legacy split "how it was paid" across DMSPayment.PaymentModeID and
# DMSOrder.PaymentModeID with no shared master underneath either, which is
# exactly the kind of two-sources-of-truth bug this avoids.


class PaymentKind(models.TextChoices):
    ADVANCE = "advance", "Advance"
    RENTAL = "rental", "Rental"
    REFUND = "refund", "Refund"


class Payment(TimeStampedModel):
    # Server-generated (unlike Order/OrderItem's device-sent sync_id): a
    # Payment row is always created as a byproduct of another already-
    # idempotent operation (order creation, keyed on Order.id), so it has no
    # need yet for its own client-supplied identity.
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    kind = models.CharField(max_length=10, choices=PaymentKind.choices)
    mode = models.ForeignKey("company.PaymentMode", on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # Cheque no / card reference / gateway ref, one pair for all of them --
    # legacy kept ChequeNo/ChequeDate and CardNumber/CardDate as separate
    # columns on DMSPayment for what is structurally the same fact.
    reference_no = models.CharField(max_length=50, blank=True)
    reference_date = models.DateTimeField(null=True, blank=True)

    device = models.ForeignKey(
        "devices.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="payments",
    )
    collected_by = models.ForeignKey(
        "core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
    )
    collected_at = models.DateTimeField()
    remarks = models.TextField(blank=True)

    class Meta:
        db_table = "payment"
        ordering = ["-collected_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_amount_positive"),
        ]
        indexes = [models.Index(fields=["order"])]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount} on {self.order}"
