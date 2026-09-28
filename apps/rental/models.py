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
