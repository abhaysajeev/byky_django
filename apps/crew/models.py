"""Crew Management.

Built from design/hrms_schema.pdf, split across four tables on the team lead's
review rather than the one the schema drew:

* **employee** -- identity and current state, what every list screen reads;
* **employee_detail** -- passport, visa, Emirates ID, labour card, bank. Rarely
  read, sensitive, and empty for every row in the client's own data;
* **employee_designation** -- the postings history. A designation changes, and
  overwriting it loses the answer to "what was this person in March", which
  payroll and incentive rules need;
* **employee_address** -- one row per address, typed. This supersedes both the
  two address columns on Employee and the separate temporary-address table.

The person entering data still sees one form; the server writes the four
together in one transaction (apps/crew/services.py).
"""

from django.db import models

from core.enums import ApprovalStatus  # noqa: F401  (documents the shared pattern)
from core.ids import uuid7
from core.models import ApprovalMixin, TimeStampedModel


class AttendanceMethod(models.TextChoices):
    QR_STATION = "qr_station", "QR scanned at the station"
    SELF = "self", "Self check-in"
    NOT_REQUIRED = "not_required", "Not required"


class AttendanceSource(models.TextChoices):
    STATION_SCAN = "station_scan", "Scanned at the station"
    SELF = "self", "Self check-in"


class Gender(models.TextChoices):
    MALE = "male", "Male"
    FEMALE = "female", "Female"
    OTHER = "other", "Other"


class MaritalStatus(models.TextChoices):
    SINGLE = "single", "Single"
    MARRIED = "married", "Married"
    WIDOWED = "widowed", "Widowed"
    DIVORCED = "divorced", "Divorced"


class AddressType(models.TextChoices):
    UAE = "uae", "UAE address"
    HOME_COUNTRY = "home_country", "Home country address"
    TEMPORARY = "temporary", "Temporary address"


class BlockAction(models.TextChoices):
    BLOCK = "block", "Blocked"
    UNBLOCK = "unblock", "Unblocked"


class RosterCategory(models.TextChoices):
    """Which designations the duty roster staffs.

    Client decision (20 Sep 2026): the roster covers Cashier and Labour only --
    every other designation (Manager, Controller, Supervisor, ...) never
    appears in its employee picker. Blank means "not a roster designation".
    """

    CASHIER = "cashier", "Cashier"
    LABOUR = "labour", "Labour"


class Designation(ApprovalMixin, TimeStampedModel):
    """A job title. Derived from the client's own Profession column."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="designations"
    )
    code = models.CharField("Designation Code", max_length=20)
    title = models.CharField("Designation Title", max_length=100)
    description = models.TextField(blank=True)
    # Which duty-roster slot this job fills, or blank for a designation the
    # roster never staffs (design/duty roster/duty-roster.md).
    roster_category = models.CharField(
        max_length=16, choices=RosterCategory.choices, blank=True
    )

    # Seniority, lower is more senior. No ranking has been supplied, so every
    # designation starts at 1 and nothing keys off it yet.
    rank_order = models.PositiveSmallIntegerField(default=1)

    # How this job's attendance is taken, unless the employee overrides it.
    attendance_method = models.CharField(
        max_length=20, choices=AttendanceMethod.choices, default=AttendanceMethod.QR_STATION
    )

    class Meta:
        db_table = "designation"
        ordering = ["rank_order", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="uniq_designation_code_per_company",
                violation_error_message="A designation with this code already exists.",
            ),
            models.UniqueConstraint(
                fields=["company", "title"], name="uniq_designation_title_per_company",
                violation_error_message="A designation with this title already exists.",
            ),
        ]
        indexes = [models.Index(fields=["company"])]

    def __str__(self):
        return self.title


class Employee(ApprovalMixin, TimeStampedModel):
    """A member of staff: who they are and where they stand today."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="employees"
    )
    employee_code = models.CharField("Employee Code", max_length=20)
    photo = models.ImageField(upload_to="employees/", blank=True)

    first_name = models.CharField("First Name", max_length=100)
    middle_name = models.CharField("Middle Name", max_length=100, blank=True)
    last_name = models.CharField("Last Name", max_length=100, blank=True)

    # The current posting. Its history lives in EmployeeDesignation, and
    # services.set_designation keeps the two in step.
    designation = models.ForeignKey(
        Designation, on_delete=models.PROTECT, related_name="employees"
    )
    branch = models.ForeignKey(
        "company.Branch", null=True, blank=True,
        on_delete=models.PROTECT, related_name="employees",
    )

    # Empty means "whatever this designation says".
    attendance_method = models.CharField(
        max_length=20, choices=AttendanceMethod.choices, blank=True
    )

    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    marital_status = models.CharField(max_length=10, choices=MaritalStatus.choices, blank=True)
    # Text, not a master: nobody has asked to filter or report by nationality,
    # and a lookup table nobody maintains goes stale.
    nationality = models.CharField(max_length=60, blank=True)

    mobile = models.CharField("Mobile Number", max_length=20, blank=True)
    email = models.EmailField("Email Address", blank=True)
    emergency_contact_person = models.CharField(max_length=100, blank=True)
    emergency_phone = models.CharField(max_length=20, blank=True)

    # Blocked is a person; is_active is the record. HR blocks staff in
    # Block/Unblock, and a blocked employee cannot sign in on any app
    # (design/03-login.md section 4).
    is_blocked = models.BooleanField(default=False)

    class Meta:
        db_table = "employee"
        ordering = ["employee_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "employee_code"], name="uniq_employee_code_per_company",
                violation_error_message="An employee with this code already exists.",
            ),
        ]
        indexes = [
            models.Index(fields=["company"]),
            models.Index(fields=["branch"]),
            models.Index(fields=["designation"]),
        ]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self):
        return " ".join(part for part in (self.first_name, self.middle_name, self.last_name) if part)

    @property
    def attendance_rule(self):
        """Their own method, or the designation's."""
        return self.attendance_method or self.designation.attendance_method


class EmployeeDetail(TimeStampedModel):
    """Documents and bank. One row per employee, kept apart because these are
    the fields that are sensitive and least often read."""

    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name="detail")

    passport_number = models.CharField(max_length=30, blank=True)
    passport_expiry = models.DateField(null=True, blank=True)
    visa_number = models.CharField(max_length=30, blank=True)
    visa_expiry = models.DateField(null=True, blank=True)
    emirates_id_number = models.CharField(max_length=30, blank=True)
    emirates_id_expiry = models.DateField(null=True, blank=True)
    labour_card_number = models.CharField(max_length=30, blank=True)
    labour_card_expiry = models.DateField(null=True, blank=True)

    salary_bank = models.CharField(max_length=100, blank=True)
    salary_account = models.CharField(max_length=100, blank=True)

    class Meta:
        db_table = "employee_detail"

    def __str__(self):
        return f"Documents for {self.employee}"


class EmployeeDesignation(TimeStampedModel):
    """One row per posting. The open row -- to_date empty -- is the current one."""

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="postings")
    designation = models.ForeignKey(Designation, on_delete=models.PROTECT, related_name="postings")
    branch = models.ForeignKey(
        "company.Branch", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    from_date = models.DateField()
    to_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "employee_designation"
        ordering = ["-from_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee"], condition=models.Q(to_date__isnull=True),
                name="uniq_open_posting_per_employee",
                violation_error_message="This employee already has a current posting.",
            ),
        ]
        indexes = [models.Index(fields=["employee", "to_date"])]

    def __str__(self):
        return f"{self.employee} as {self.designation} from {self.from_date}"


class EmployeeAddress(TimeStampedModel):
    """Where someone lives. Typed, so the UAE address, the home-country address
    and a temporary address are the same shape rather than three schemas."""

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="addresses")
    address_type = models.CharField(max_length=20, choices=AddressType.choices)

    line1 = models.CharField("Address Line 1", max_length=255)
    line2 = models.CharField("Address Line 2", max_length=255, blank=True)
    building = models.CharField("Building Name", max_length=100, blank=True)
    flat = models.CharField("Flat / Room No", max_length=50, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.ForeignKey(
        "company.State", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    country = models.ForeignKey(
        "company.Country", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    zip_code = models.CharField("Zip Code", max_length=20, blank=True)

    landlord_name = models.CharField(max_length=100, blank=True)
    landlord_phone = models.CharField(max_length=20, blank=True)
    contact_person = models.CharField(max_length=100, blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)

    is_current = models.BooleanField(default=True)

    class Meta:
        db_table = "employee_address"
        ordering = ["employee", "address_type"]
        indexes = [models.Index(fields=["employee", "address_type"])]

    def __str__(self):
        return f"{self.get_address_type_display()} for {self.employee}"


class EmployeeBlockLog(TimeStampedModel):
    """Why someone was blocked, and when they came back.

    A log, so it carries no approval fields and is never edited.
    """

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="block_log")
    action = models.CharField(max_length=10, choices=BlockAction.choices)
    reason = models.CharField(max_length=100, blank=True)
    effective_date = models.DateField()
    remarks = models.TextField(blank=True)

    class Meta:
        db_table = "employee_block_log"
        ordering = ["-effective_date", "-created_on"]
        indexes = [models.Index(fields=["employee"])]

    def __str__(self):
        return f"{self.employee} {self.get_action_display().lower()} on {self.effective_date}"


class Attendance(TimeStampedModel):
    """One punch.

    A transaction, not a master: created on a device in the field, so it carries
    a UUIDv7 key and a business_date (design/00-findings.md sections 8 and 9).
    Login no longer writes attendance, unlike the legacy.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="attendance")
    user = models.ForeignKey(
        "core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="attendance"
    )
    source = models.CharField(max_length=20, choices=AttendanceSource.choices)

    punched_at = models.DateTimeField()
    # Which day it counts as, in the company's timezone. Written once, never
    # recomputed -- an offline punch made at 00:20 and synced at 08:00 belongs
    # to the day it happened.
    business_date = models.DateField()

    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    device = models.ForeignKey(
        "devices.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="attendance"
    )
    # Required for a station scan, empty for a self check-in.
    branch = models.ForeignKey(
        "company.Branch", null=True, blank=True, on_delete=models.PROTECT, related_name="attendance"
    )
    scanned_by = models.ForeignKey(
        "core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    qr_token_id = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "attendance"
        ordering = ["-punched_at"]
        constraints = [
            # A QR token is single use: scanning the same code twice records one
            # punch, not two.
            models.UniqueConstraint(
                fields=["qr_token_id"], condition=~models.Q(qr_token_id=""),
                name="uniq_attendance_qr_token",
                violation_error_message="That QR code has already been scanned.",
            ),
        ]
        indexes = [
            models.Index(fields=["employee", "business_date"]),
            models.Index(fields=["business_date"]),
            models.Index(fields=["branch", "business_date"]),
        ]

    def __str__(self):
        return f"{self.employee} at {self.punched_at}"


class DutyRosterDayType(models.TextChoices):
    """One employee's one day, exactly as the client's mockup records it
    (design/duty roster/business-logic.md section 1). Leave here is typed
    directly -- there is no approval workflow yet; see DutyRoster's docstring."""

    WORKING = "working", "Working"
    WEEK_OFF = "week_off", "Week Off"
    SICK_LEAVE = "sick_leave", "Sick Leave"
    CASUAL_LEAVE = "casual_leave", "Casual Leave"


class DutyRoster(TimeStampedModel):
    """One employee's plan for one day: where, and on which shift.

    Not a master -- no ApprovalMixin. It is a plan an admin writes and edits
    directly, the way the client's own "branch-first" flow already works
    ("changes save immediately; there is no draft here").

    Employees are not branch-mapped (client decision, 20 Sep 2026):
    `Employee.branch` stays as a home/base fact used elsewhere (Attendance's
    fallback branch, HR screens), but this table never reads it. Every day
    picks its own branch, because Cashier and Labour staff move between
    branches as staffing needs it.

    Shift times come from `company.BranchWorkingTime` for the branch and
    weekday picked -- `services.branch_shift_defaults` -- prefilled and still
    editable, never typed from nothing. Both ends of a shift carry their own
    date (DateTimeField, not TimeField) so an overnight shift is just an end
    that falls on the next calendar day; no flag is needed.

    Two features read against this table are not built yet, by the client's
    own scoping for the Oct 2 demo, and are recorded here so adding them later
    is additive, not a rewrite:

    * **Leave Request.** Once it exists, an *approved* request for an
      employee/date should override what this table's own `day_type` says for
      that day when it is displayed or read by login -- see the TODO in
      `services.today_assignment`. This table's own Sick/Casual Leave values
      stay as the fallback for a day nobody has filed a request for.
    * **Attendance-derived Absent.** A `working` day with no matching
      `Attendance` row should read as Absent. Not stored here -- it is always
      computed by comparing the two tables, documented as
      `services.absent_today` (not implemented).
    """

    employee = models.ForeignKey(
        "crew.Employee", on_delete=models.PROTECT, related_name="duty_roster"
    )
    date = models.DateField()
    day_type = models.CharField(max_length=16, choices=DutyRosterDayType.choices)

    branch = models.ForeignKey(
        "company.Branch", null=True, blank=True,
        on_delete=models.PROTECT, related_name="duty_roster",
    )
    shift1_start = models.DateTimeField(null=True, blank=True)
    shift1_end = models.DateTimeField(null=True, blank=True)
    shift2_start = models.DateTimeField(null=True, blank=True)
    shift2_end = models.DateTimeField(null=True, blank=True)

    remarks = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "duty_roster"
        verbose_name_plural = "duty roster"
        ordering = ["date", "employee"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "date"], name="uniq_duty_roster_employee_date",
                violation_error_message="This employee already has a roster entry for that day.",
            ),
            # "No branch / shift required for this day type" (the mockup's own
            # words) as a database guarantee: Working carries a branch and
            # Shift 1; nothing else carries either.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        day_type=DutyRosterDayType.WORKING,
                        branch__isnull=False, shift1_start__isnull=False, shift1_end__isnull=False,
                    )
                    | (
                        ~models.Q(day_type=DutyRosterDayType.WORKING)
                        & models.Q(
                            branch__isnull=True,
                            shift1_start__isnull=True, shift1_end__isnull=True,
                            shift2_start__isnull=True, shift2_end__isnull=True,
                        )
                    )
                ),
                name="duty_roster_working_needs_branch_and_shift1",
            ),
            models.CheckConstraint(
                condition=models.Q(shift1_end__gt=models.F("shift1_start")),
                name="duty_roster_shift1_end_after_start",
            ),
            # Shift 2 is both fields or neither, and never without Shift 1.
            models.CheckConstraint(
                condition=(
                    models.Q(shift2_start__isnull=True, shift2_end__isnull=True)
                    | models.Q(
                        shift2_start__isnull=False, shift2_end__isnull=False,
                        shift1_start__isnull=False,
                        shift2_end__gt=models.F("shift2_start"),
                    )
                ),
                name="duty_roster_shift2_both_or_neither_after_shift1",
            ),
        ]
        indexes = [
            models.Index(fields=["employee", "date"]),
            models.Index(fields=["branch", "date"]),
            models.Index(fields=["date"]),
        ]

    def __str__(self):
        return f"{self.employee} on {self.date}: {self.get_day_type_display()}"
