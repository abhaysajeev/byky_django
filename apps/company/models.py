"""Company Management.

Built from design/02-company.md and design/company_schema_day1.pdf.
Hierarchy: Company -> Country -> State -> Location -> Branch, where a Branch is
what the business calls a station.

BranchApprovalAuthority is designed here but lands with the crew module, since
it points at Employee (design/02-company.md sign-off).
"""

from django.db import models

from core.models import ApprovalMixin, TimeStampedModel


class BranchType(models.TextChoices):
    HEAD_OFFICE = "head_office", "Head Office"
    DEPOT = "depot", "Depot"
    STATION = "station", "Station"


class WeekDay(models.IntegerChoices):
    """Python's own numbering, `date.weekday()`: Monday is 0, Sunday is 6.

    One numbering everywhere -- models, services, fares, the apps -- so nothing
    has to translate between Python and the database. Screens still list the
    UAE week, Sunday first: see UAE_WEEK.
    """

    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3, "Thursday"
    FRIDAY = 4, "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


# The order screens show the days in: the UAE week runs Sunday to Saturday.
# Display order only -- the stored value is always WeekDay's.
UAE_WEEK = (
    WeekDay.SUNDAY, WeekDay.MONDAY, WeekDay.TUESDAY, WeekDay.WEDNESDAY,
    WeekDay.THURSDAY, WeekDay.FRIDAY, WeekDay.SATURDAY,
)


class TaxType(models.TextChoices):
    """Whether the fare a customer sees already contains the tax.

    Legacy CoreCompany.TaxType: 0 = included in the basic fare, 1 = excluded.
    """

    INCLUDED = "included", "Included in basic fare"
    EXCLUDED = "excluded", "Excluded from basic fare"


class DiscountType(models.TextChoices):
    """Whether a discount comes off the fare before or after tax is applied.

    Legacy CoreCompany.Discount: 0 = before tax, 1 = after tax.
    """

    BEFORE_TAX = "before_tax", "Before tax"
    AFTER_TAX = "after_tax", "After tax"


class AuthorityType(models.TextChoices):
    LEAVE_REQUEST = "leave_request", "Leave Request"
    MAINTENANCE = "maintenance", "Service & Maintenance"
    RMS_APP_REQUEST = "rms_app_request", "RMS App Request"


class Country(ApprovalMixin, TimeStampedModel):
    short_code = models.CharField("Country Code", max_length=10, unique=True)
    name = models.CharField("Country Name", max_length=100, unique=True)

    class Meta:
        db_table = "country"
        verbose_name_plural = "countries"
        ordering = ["name"]

    def __str__(self):
        return self.name


class State(ApprovalMixin, TimeStampedModel):
    """An emirate, in the UAE."""

    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="states")
    short_code = models.CharField("State Code", max_length=10)
    name = models.CharField("State Name", max_length=100)

    class Meta:
        db_table = "state"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["country", "short_code"], name="uniq_state_code_per_country",
                violation_error_message="This country already has a state with that code.",
            ),
        ]
        indexes = [models.Index(fields=["country"])]

    def __str__(self):
        return self.name


class Location(ApprovalMixin, TimeStampedModel):
    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="locations")
    state = models.ForeignKey(State, on_delete=models.PROTECT, related_name="locations")
    # 20, not 10: the live data has codes up to 13 characters
    # ("bykystation10", "duriverparks"). Truncating to 10 would have collided
    # "bykystation1" with "bykystation10".
    short_code = models.CharField("Location Code", max_length=20)
    name = models.CharField("Location Name", max_length=100)
    landmark = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "location"
        ordering = ["name"]
        indexes = [models.Index(fields=["state"]), models.Index(fields=["country"])]

    def __str__(self):
        return self.name


class Company(ApprovalMixin, TimeStampedModel):
    """Was one row in the legacy; multi-company is now a real requirement
    (design/00-findings.md sections 1-2)."""

    short_code = models.CharField("Company Code", max_length=10, unique=True)
    name = models.CharField("Company Name", max_length=255)

    # One timezone per company; Branch carries none (design/00-findings.md
    # section 7). An IANA name, never an offset.
    timezone = models.CharField(max_length=40, default="Asia/Dubai")

    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="+")
    state = models.ForeignKey(State, on_delete=models.PROTECT, related_name="+")
    address = models.CharField(max_length=500, blank=True)
    city = models.CharField(max_length=100, blank=True)
    zip_code = models.CharField(max_length=20, blank=True)

    ceo_name = models.CharField(max_length=100, blank=True)
    dto_name = models.CharField(max_length=100, blank=True)
    contact_person = models.CharField(max_length=100, blank=True)
    date_of_commissioning = models.DateField(null=True, blank=True)

    phone_number = models.CharField(max_length=20)
    fax_number = models.CharField(max_length=20, blank=True)
    email = models.EmailField()
    website = models.URLField(blank=True)
    logo = models.ImageField(upload_to="company/", blank=True)

    incorporation_certificate_number = models.CharField(max_length=100, blank=True)
    business_certificate_number = models.CharField(max_length=100, blank=True)
    tax_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # How a fare is priced. Company-level, not per station: the legacy stored
    # them on every device-settings row but Save_Company_TaxSEttings overwrote
    # them all from the company whenever it was saved, so they were only ever
    # one value. Defaults follow the legacy tablet proc's ISNULL (0 = included,
    # 0 = before tax).
    tax_type = models.CharField(max_length=16, choices=TaxType.choices, default=TaxType.INCLUDED)
    discount_type = models.CharField(
        max_length=16, choices=DiscountType.choices, default=DiscountType.BEFORE_TAX
    )

    # Indian tax-regime fields, kept and shown conditionally on country
    # (design/02-company.md section 3.5).
    income_tax_number = models.CharField(max_length=100, blank=True)
    service_tax_number = models.CharField(max_length=100, blank=True)
    tin = models.CharField(max_length=100, blank=True)
    cst = models.CharField(max_length=100, blank=True)

    class Meta:
        db_table = "company"
        verbose_name_plural = "companies"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Department(ApprovalMixin, TimeStampedModel):
    """A company's own structure -- Operations, Workshop, Accounts.

    Company-scoped, so two companies can each have an "Operations" department
    (added in revision 5 of design/02-company.md; the first pass made the name
    unique system-wide, which multi-company makes wrong).
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="departments")
    short_code = models.CharField("Department Code", max_length=10)
    name = models.CharField("Department Name", max_length=100)

    class Meta:
        db_table = "department"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "short_code"], name="uniq_department_code_per_company",
                violation_error_message="A department with this code already exists.",
            ),
            models.UniqueConstraint(
                fields=["company", "name"], name="uniq_department_name_per_company",
                violation_error_message="A department with this name already exists.",
            ),
        ]
        indexes = [models.Index(fields=["company"])]

    def __str__(self):
        return self.name


class Branch(ApprovalMixin, TimeStampedModel):
    """A station. Every rental, device and employee hangs off one."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="branches")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="branches")
    branch_type = models.CharField(max_length=20, choices=BranchType.choices, default=BranchType.STATION)
    # Unique per company (below), not globally: two companies on one server can
    # each have their own "AUH01". Multi-company arrived after this field did
    # (design/03-login.md section 9B.2); this constraint closes the gap.
    short_code = models.CharField("Branch Code", max_length=10)
    name = models.CharField("Branch Name", max_length=100)

    is_multi_device = models.BooleanField(default=False)
    allows_test_ride = models.BooleanField(default=False)
    is_hotel = models.BooleanField(default=False)
    accepts_app_payment = models.BooleanField(default=False)
    hotel_commission = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    departments = models.ManyToManyField(Department, blank=True, related_name="branches")

    image = models.ImageField(upload_to="branches/", blank=True)
    station_number = models.CharField(max_length=50, blank=True)
    address = models.CharField(max_length=500, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    contact_no = models.CharField(max_length=20, blank=True)

    class Meta:
        db_table = "branch"
        verbose_name_plural = "branches"
        ordering = ["name"]
        constraints = [
            # Replaces the legacy ValidateDupHO check-then-insert race.
            models.UniqueConstraint(
                fields=["company", "branch_type"],
                condition=models.Q(branch_type=BranchType.HEAD_OFFICE, is_active=True),
                name="uniq_active_head_office_per_company",
                violation_error_message="This company already has an active head office.",
            ),
            models.UniqueConstraint(
                fields=["company", "short_code"],
                name="uniq_branch_code_per_company",
                violation_error_message="A branch with this code already exists.",
            ),
            # Redundant as a uniqueness rule -- id alone is unique -- but it is
            # what lets device_settings' composite foreign key point here, so a
            # settings row's copied company can never disagree with its branch's
            # (migration devices/0012, the same pattern as app_release_mapping's
            # in devices/0007).
            models.UniqueConstraint(
                fields=["id", "company"],
                name="uniq_branch_id_company",
            ),
        ]
        indexes = [models.Index(fields=["company"]), models.Index(fields=["location"])]

    def __str__(self):
        return self.name


class BranchWorkingTime(ApprovalMixin, TimeStampedModel):
    """The weekly shift matrix: up to four shifts a day, seven days.

    Plain local times, read in the company's timezone. Never converted to UTC --
    a clock rule is not a point in time (design/00-findings.md section 7).
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="working_times")
    week_day = models.PositiveSmallIntegerField(choices=WeekDay.choices)
    shift_number = models.PositiveSmallIntegerField(default=1)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        db_table = "branch_working_time"
        ordering = ["branch", "week_day", "shift_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "week_day", "shift_number"],
                name="uniq_branch_weekday_shift",
                violation_error_message="That shift is already set for this day.",
            ),
            models.CheckConstraint(
                condition=models.Q(shift_number__gte=1, shift_number__lte=4),
                name="branch_working_time_shift_1_to_4",
            ),
        ]

    def __str__(self):
        return f"{self.branch} {self.get_week_day_display()} shift {self.shift_number}"


class BranchApprovalAuthority(TimeStampedModel):
    """Who approves what at a branch.

    One table with an authority_type rather than three columns, so a fourth
    domain later needs no schema change (design/02-company.md 3.3). Deferred
    from that module's sign-off until crew.Employee existed.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="approval_authority")
    employee = models.ForeignKey(
        "crew.Employee", on_delete=models.PROTECT, related_name="approval_authority"
    )
    authority_type = models.CharField(max_length=20, choices=AuthorityType.choices)

    class Meta:
        db_table = "branch_approval_authority"
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "employee", "authority_type"],
                name="uniq_branch_employee_authority",
                violation_error_message="That person already holds this authority at this branch.",
            ),
        ]
        indexes = [models.Index(fields=["branch"])]

    def __str__(self):
        return f"{self.employee} approves {self.get_authority_type_display()} at {self.branch}"
