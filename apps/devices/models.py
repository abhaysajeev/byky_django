"""Registered tablets and phones, and the app releases they run.

Built from design/03-login.md sections 8.3 to 8.6 and
design/registration/registration-api.md. No MAC addresses: modern Android will
not give an app the device's MAC, so the app generates its own id on first
install (design/01-auth.md section 1.1).

Four identifiers, each with its own job:

    id                      the primary key. What every FK points at, and the
                            only one that never leaves the server.
    installation_id         the UUID the app generates on first launch and keeps
                            in secure storage. *The* identity.
    device_registration_id  the number the app stores and support quotes
                            ("device 1548"). Issued at registration, before any
                            approval, and kept for the life of the tablet.
    platform_id             ANDROID_ID / identifierForVendor. A hint used to
                            recognise a reinstall, never an identity: it is a
                            value the app sends, so it can be wrong or forged.

Because the FKs point at `id`, a confirmed reconnection can move a new
installation_id onto the original row and keep both its id and its
device_registration_id, so sessions and attendance stay correct.
"""

from decimal import Decimal

from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Func, Q, Value

from core.enums import Channel
from core.models import ApprovalMixin, TimeStampedModel

# The sequence behind device_registration_id, created in migration 0003. It is
# deliberately not the table's primary key: this number is printed on a waiting
# screen and read down a phone line, so it must not move if the key ever does.
REGISTRATION_SEQUENCE = "device_registration_seq"


class DeviceStatus(models.TextChoices):
    PENDING = "pending", "Awaiting approval"
    APPROVED = "approved", "Approved"
    BLOCKED = "blocked", "Blocked"
    RETIRED = "retired", "Retired"


class Platform(models.TextChoices):
    ANDROID = "android", "Android"
    IOS = "ios", "iOS"


class DeviceAction(models.TextChoices):
    """What an admin did to a device. The rows of DeviceStatusLog."""

    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    BLOCKED = "blocked", "Blocked"
    UNBLOCKED = "unblocked", "Unblocked"
    RECONNECTED = "reconnected", "Reconnected after reinstall"
    REPLACED = "replaced", "Replaced an earlier device"
    RETIRED = "retired", "Retired"


class Device(TimeStampedModel):
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="devices")

    installation_id = models.CharField(max_length=64, unique=True)
    device_registration_id = models.BigIntegerField(
        unique=True,
        editable=False,
        db_default=Func(Value(REGISTRATION_SEQUENCE), function="nextval"),
    )

    platform = models.CharField(max_length=8, choices=Platform.choices, default=Platform.ANDROID)
    # A hint for spotting a reinstall, nothing more. Deliberately not unique:
    # the three apps on one phone report the same value, and a reinstall leaves
    # the old row behind holding it.
    platform_id = models.CharField(max_length=64, blank=True)
    device_model = models.CharField(max_length=120, blank=True)

    # Set by the admin at approval; a device that has never been approved has no
    # name, which is why this stays blank-able.
    name = models.CharField(max_length=120, blank=True)
    channel = models.CharField(max_length=16, choices=Channel.choices)

    # There is no branch column here on purpose. Which station a device trades
    # at, and since when, is DeviceMapping's whole job -- the open row is the
    # current station. Keeping a copy here would mean two writers for one fact,
    # and at 84 stations the join it saves is not worth the risk of the two
    # disagreeing. Read it through `current_branch` below.

    status = models.CharField(max_length=16, choices=DeviceStatus.choices, default=DeviceStatus.PENDING)

    # The factory-reset case: both identifiers changed, so this is a new row
    # standing in for an old one (design/03-login.md section 9B.4).
    replaced_device = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replaced_by",
    )
    # The reinstall case: a pending request that looks like an existing device.
    # The matched device is left untouched until an admin confirms, so a forged
    # platform_id cannot put a working station into a pending state (9B.3).
    reconnect_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="reconnect_requests",
    )

    # FCM token, sent as credentials.device_notification_id. Saved before
    # approval because that is the only moment the app has offered it, and it is
    # what lets the server nudge the tablet the moment an admin approves.
    push_token = models.CharField(max_length=512, blank=True)

    # Last known position, from the latitude/longitude the app sends in its
    # credentials block on every call. Overwritten each time: this is where the
    # tablet was, not where it has been. A trail of positions is the tracking
    # module's job -- the legacy kept 80,676 of them in DMSGPSDetails.
    #
    # The legacy carried these on the mapping row, where they meant "where the
    # operator stood at login" and never changed again. A station's own
    # coordinates are on Branch.
    #
    # last_location_at is not optional in spirit: coordinates with no timestamp
    # cannot be judged, because "at the station yesterday" and "at the station
    # in 2024" read identically.
    last_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    last_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    last_location_at = models.DateTimeField(null=True, blank=True)

    approved_by = models.ForeignKey(
        "core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "device"
        ordering = ["name", "device_registration_id"]
        indexes = [
            models.Index(fields=["company"]),
            models.Index(fields=["status"]),
            # The reinstall lookup: platform_id within one app and company.
            models.Index(fields=["platform_id", "channel", "company"]),
        ]
        constraints = [
            # approved_by is deliberately absent here: it is SET_NULL, so naming
            # it would make anyone who ever approved a device undeletable. The
            # service requires it; the database records only that it happened.
            models.CheckConstraint(
                condition=~Q(status=DeviceStatus.APPROVED) | Q(approved_at__isnull=False),
                name="device_approved_has_approval_time",
            ),
            # "An approved operator device must have a station" cannot live here
            # any more: the station is in another table, and a check constraint
            # cannot read one. The approval service enforces it, alongside the
            # multi-device rule, which was never expressible here either.
        ]

    def __str__(self):
        return self.name or f"Device {self.device_registration_id}"

    @property
    def is_usable(self):
        return self.status == DeviceStatus.APPROVED

    @property
    def open_mapping(self):
        """The mapping row in force now, or None if the device is unmapped."""
        return self.mappings.filter(to_date__isnull=True).select_related("branch").first()

    @property
    def current_branch(self):
        """The station this device trades at now, or None.

        One query per device, so a list view should join the open mappings
        itself rather than read this in a loop.
        """
        mapping = self.open_mapping
        return mapping.branch if mapping else None


class DeviceTrust(TimeStampedModel):
    """"Remember device". Proves the device, never the person's rights: the
    login still runs every check (design/03-login.md section 6.4)."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="trusts")
    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="device_trusts")
    token_hash = models.CharField(max_length=128, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "device_trust"
        indexes = [models.Index(fields=["user"])]

    def __str__(self):
        return f"{self.user} on {self.device}"


class DeviceMapping(TimeStampedModel):
    """Which station a device was at, and since when.

    Replaces the assignment half of the legacy SfaDeviceMapping. Its login half
    -- LoginTime, LogoutTime, UserID -- is AppSession, and its odometer columns
    are not carried: they were hidden in the legacy screen and never held
    anything but 0.

    Re-mapping closes the open row and inserts a new one; nothing is ever
    updated in place, which is what the legacy did too
    (DeviceMappingController.cs:289-324). Intervals are half-open --
    [from_date, to_date) -- so a closed row's to_date is the next row's
    from_date.

    Both are moments, not days, and nobody types them: mapping stamps now, and
    a re-map or close stamps the old row's to_date with the same moment --
    exactly what the legacy did (DeviceMappingModels.cs:235, controller :315).
    Times keep two moves in one day in order; the names stay from_date /
    to_date after the legacy's FromDate / ToDate, which were datetimes too.

    An assignment table, so no approval fields: a mapping is not separately
    approved. The legacy's mapping-level Reject is also where its worst bug
    lived -- it cleared IsActive but left ToDate null, so the device stayed
    un-mappable for ever (DeviceMappingModels.cs:321).
    """

    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name="mappings")
    branch = models.ForeignKey(
        "company.Branch", on_delete=models.PROTECT, related_name="device_mappings"
    )
    from_date = models.DateTimeField()
    to_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "device_mapping"
        ordering = ["-from_date", "-id"]
        constraints = [
            # One open mapping per device. The legacy had no such rule, which is
            # why five stored procedures each pick "the current mapping" with a
            # different tie-breaker. One open row makes the question answerable.
            models.UniqueConstraint(
                fields=["device"],
                condition=Q(to_date__isnull=True),
                name="uniq_open_mapping_per_device",
            ),
            models.CheckConstraint(
                condition=Q(to_date__isnull=True) | Q(to_date__gte=F("from_date")),
                name="device_mapping_dates_in_order",
            ),
        ]
        indexes = [
            models.Index(fields=["branch", "to_date"]),
            models.Index(fields=["device", "to_date"]),
        ]

    def __str__(self):
        return f"{self.device} at {self.branch} from {self.from_date}"

    @property
    def is_open(self):
        return self.to_date is None


class DeviceStatusLog(TimeStampedModel):
    """Why a device was approved, rejected, blocked or replaced.

    A log, on the same footing as crew's EmployeeBlockLog: never edited, no
    approval fields, and who and when come from the audit stamps.

    Mapping changes are deliberately not logged here -- DeviceMapping is already
    the mapping history, with its own stamps. One event stream and one interval
    table, not two that half overlap.
    """

    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name="status_log")
    action = models.CharField(max_length=16, choices=DeviceAction.choices)
    from_status = models.CharField(max_length=16, choices=DeviceStatus.choices, blank=True)
    to_status = models.CharField(max_length=16, choices=DeviceStatus.choices)
    reason = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)
    # The other device in a reconnection or a replacement.
    related_device = models.ForeignKey(
        Device, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )

    class Meta:
        db_table = "device_status_log"
        ordering = ["-created_on", "-id"]
        indexes = [models.Index(fields=["device"])]

    def __str__(self):
        return f"{self.device} {self.get_action_display().lower()}"


class UpdateType(models.TextChoices):
    """The dropdown on a release: must the tablet install it, or may it wait."""

    MANDATORY = "mandatory", "Mandatory"
    ANYTIME = "anytime", "Any time"


class ReleaseScope(models.TextChoices):
    ALL = "all", "All branches"
    BRANCH = "branch", "One branch"


VERSION_NAME = RegexValidator(
    r"^\d+\.\d+\.\d+$", "Use digits and dots only, like 1.12.80."
)


class AppRelease(TimeStampedModel):
    """One APK build: which app, which version, and where to download it.

    Replaces Sfa_APKUpdation and the 18 version strings hardcoded in the legacy
    operator login. A release says what the build *is*; which branches get it is
    AppReleaseMapping's job. Uploading a release on its own reaches nobody.

    There is deliberately no `is_default`. The legacy's IsDefault was written on
    every upload and read by nothing -- its fallback ordered by the surrogate
    key instead -- and all 12 of its live rows carried it at once. The fallback
    here is an "All branches" mapping row, which can be seen and switched off.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="app_releases"
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    # Shown to people. The legacy's free-text version was not unique: 1.12.79
    # appears twice, and rows labelled 1.12.77 and 1.12.78 point at the 1.12.68
    # binary.
    version_name = models.CharField(max_length=20, validators=[VERSION_NAME])
    # Android's versionCode, and the only value ever compared. As text, 1.12.9
    # sorts after 1.12.10.
    version_code = models.PositiveIntegerField()
    update_type = models.CharField(
        max_length=16, choices=UpdateType.choices, default=UpdateType.ANYTIME
    )
    download_url = models.URLField(max_length=500)
    update_note = models.TextField(blank=True)
    # False = withdrawn. A withdrawn release is never offered, and the branches
    # mapped to it fall back to the All-branches mapping.
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "app_release"
        ordering = ["company", "channel", "-version_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "channel", "version_code"],
                name="uniq_release_version_code",
                violation_error_message="That version code is already used for this app.",
            ),
            models.UniqueConstraint(
                fields=["company", "channel", "version_name"],
                name="uniq_release_version_name",
                violation_error_message="That version is already uploaded for this app.",
            ),
            # Redundant as a uniqueness rule -- id alone is unique -- but it is
            # what lets app_release_mapping's composite foreign key point here,
            # so a mapping's copied company and channel can never disagree with
            # the release (migration 0007).
            models.UniqueConstraint(
                fields=["id", "company", "channel"],
                name="uniq_release_id_company_channel",
            ),
            models.CheckConstraint(
                condition=Q(version_code__gt=0), name="app_release_version_code_positive"
            ),
            # Channel includes the back office; there is no APK for a browser.
            models.CheckConstraint(
                condition=~Q(channel=Channel.WEB), name="app_release_channel_is_an_app"
            ),
        ]

    def __str__(self):
        return f"{self.get_channel_display()} {self.version_name}"

    @property
    def is_mandatory(self):
        """The boolean the update-check response carries."""
        return self.update_type == UpdateType.MANDATORY


class AppReleaseMapping(TimeStampedModel):
    """Which branches get which release.

    A row targets either one branch or all of them. Resolving a device's build
    takes its branch's row first and the All row otherwise, so switching a
    branch's row off drops that branch back to the All-branches build.

    Replaces SfaApkMapping, minus its faults: closing a mapping there overwrote
    CreatedOn and CreatedBy, its Delete and Reject did the same thing, approval
    was simply "was the creator an admin", and its grid passed the release id
    where the mapping id was expected.

    Rows are never deleted -- unticking a branch switches its row off -- so the
    history of which build a branch was on survives. That is also why there is
    no unique (release, branch): mapping, unmapping and re-mapping the same pair
    is three legitimate rows.

    `company` and `channel` are copies of the release's, held only so the unique
    index below can see them: an index cannot reach across to app_release. A
    release's company and app never change, and a composite foreign key
    (migration 0007) makes Postgres refuse any row whose copy disagrees.
    """

    release = models.ForeignKey(AppRelease, on_delete=models.PROTECT, related_name="mappings")
    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="app_release_mappings"
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    scope = models.CharField(max_length=8, choices=ReleaseScope.choices)
    branch = models.ForeignKey(
        "company.Branch", null=True, blank=True,
        on_delete=models.PROTECT, related_name="app_release_mappings",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "app_release_mapping"
        ordering = ["company", "channel", "scope", "-created_on"]
        constraints = [
            # One index, two rules: one active mapping per branch per app, and --
            # because nulls_distinct=False makes the All rows' NULL branch collide
            # -- one active All-branches mapping per app. Without it, two All rows
            # coexist happily; that was checked in Postgres, not assumed.
            models.UniqueConstraint(
                fields=["company", "channel", "branch"],
                condition=Q(is_active=True),
                nulls_distinct=False,
                name="uniq_active_release_mapping_per_branch",
                violation_error_message="This branch already has an active release for this app.",
            ),
            models.CheckConstraint(
                condition=(
                    Q(scope=ReleaseScope.ALL, branch__isnull=True)
                    | Q(scope=ReleaseScope.BRANCH, branch__isnull=False)
                ),
                name="app_release_mapping_scope_matches_branch",
            ),
        ]
        indexes = [
            models.Index(fields=["branch", "channel", "is_active"]),
            models.Index(fields=["company", "channel", "is_active"]),
        ]

    def __str__(self):
        target = self.branch if self.branch_id else "all branches"
        return f"{self.release} -> {target}"


class PrintType(models.TextChoices):
    PORTRAIT = "portrait", "Portrait"
    LANDSCAPE = "landscape", "Landscape"


class RoundOffMode(models.TextChoices):
    """Legacy RoundOff 0/1/2. "Auto" was the legacy's word for Nearest."""

    NEAREST = "nearest", "Nearest"
    UPWARD = "upward", "Upward"
    DOWNWARD = "downward", "Downward"


# Stored as the amount itself, not a code: the legacy stored 0/1/2 and turned it
# into 0.25 / 0.50 / 1 in C# (SFA.cs:910-914). There is no 5-fils step -- the
# wireframe offers one, the legacy never had it.
ROUND_OFF_STEPS = [
    (Decimal("0.25"), "25 fils"),
    (Decimal("0.50"), "50 fils"),
    (Decimal("1.00"), "1 AED"),
]

ORDER_NO_PREFIX = RegexValidator(r"^[A-Za-z0-9]+$", "Letters and digits only.")


class DeviceSettingsQuerySet(models.QuerySet):
    """Leaves the logo behind unless it is asked for.

    A settings row is mostly short text, plus ~25 KB of bitmap. A screen listing
    every station would carry a couple of megabytes of bitmap it never shows, so
    the default queryset defers the column; `with_logo()` is how the two callers
    that need the bytes -- the tablet's settings payload and the screen's
    preview -- ask for it, and it works part-way through a chain of filters,
    which is where both of them ask. Touching `.logo` off a deferred row still
    works; it costs one extra query, the right trade for a field this size.
    """

    def with_logo(self):
        return self.defer(None)


class DeviceSettingsManager(models.Manager.from_queryset(DeviceSettingsQuerySet)):
    def get_queryset(self):
        return super().get_queryset().defer("logo")


class DeviceSettings(ApprovalMixin, TimeStampedModel):
    """How every tablet at one station prints and prices. Replaces SfaDeviceSetting.

    Per branch, not per device: a tablet finds its settings through the station
    it is mapped to, and downloads them after login. One active row per branch
    was a rule the legacy kept only in its screen; it is a database rule here.

    A master, so it carries the approval trio -- the legacy approved settings
    rows (admin-only IsApproved, with Reject).

    Of the legacy's 47 columns, the ones not here were dropped on what the code
    did with them, not on QA data. The C# mapper that built the tablet's payload
    (ERP.Interface/SFA.cs:828-918) silently dropped the whole van-sales group --
    MinSaleQty, CashDiscount, SaleType, StockVerify, IsMRPEnable,
    IsCompanyTINOrCSTEnabled, DefaultVatForm, AlertLimit -- and GPSTimeInterval.
    TransNoStartCharacter had no reader; MainDisplay1/2 had no input and were
    never rendered; BillCopy and BillPrintType were hidden. See design/03-login.md
    section 8.7 for the full list and PORTING.md for the wireframe differences.

    Tax type and discount type are on Company: the legacy copied them onto every
    row from the company on each save, so they were only ever one value.

    No history table. The legacy kept one, by trigger, so a reprinted receipt
    showed the headers of its bill date; the rental order snapshots the header
    and footer it printed instead, and a reprint reads the order.
    """

    branch = models.ForeignKey(
        "company.Branch", on_delete=models.PROTECT, related_name="device_settings"
    )
    # A copy of branch.company, held only so the unique constraints below can
    # see it -- a constraint cannot reach across to branch. Set from the branch
    # in save(), never by the caller, and a company can never change branches,
    # so the two cannot drift; the composite foreign key (migration 0012) makes
    # Postgres refuse a row where they somehow did. Same pattern as
    # AppReleaseMapping.company (migration 0007), for the same reason.
    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="device_settings"
    )
    settings_code = models.CharField("Settings Code", max_length=15)

    # Receipt text. Lengths are the legacy screen's limits.
    header_1 = models.CharField("Header 1", max_length=48)
    header_2 = models.CharField("Header 2", max_length=48)
    # Printed on every legacy receipt, and missing from the wireframe.
    additional_header_1 = models.CharField("Additional Header 1", max_length=75, blank=True)
    additional_header_2 = models.CharField("Additional Header 2", max_length=75, blank=True)
    footer_1 = models.CharField("Footer 1", max_length=75)
    footer_2_arabic = models.CharField("Footer 2 (Arabic)", max_length=48, blank=True)

    # Printing. Defaults are what every legacy row actually holds.
    # 2 in 60 of the client's 95 live rows and 4 in the other 35: a trait of the
    # printer at the station, so the commoner value is the better default.
    paper_feed = models.PositiveSmallIntegerField("Paper Feed", default=2)
    print_type = models.CharField(
        "Print Type", max_length=16, choices=PrintType.choices, default=PrintType.LANDSCAPE
    )
    print_logo = models.BooleanField("Print Logo", default=True)
    # The bytes of the bitmap, stored exactly as uploaded and never re-encoded:
    # the live logos are 1-bit BMPs 832 dots wide, which is the print head's own
    # width, so anything that "improves" the image breaks the print. The legacy
    # kept them in the row too (base64 in an ntext column); raw bytes are the
    # same picture a third smaller, and Postgres stores a value this size out of
    # line automatically. The tablet is sent it base64 with its settings, so a
    # station can print while it is offline.
    logo = models.BinaryField("Logo", blank=True, null=True, editable=True)
    logo_name = models.CharField("Logo File", max_length=120, blank=True)
    # The tablet re-downloads the logo only when this has moved past its last
    # sync (Service_Get_Device_SEttings.sql:46-50), so it must change whenever
    # the logo does.
    logo_changed_on = models.DateTimeField(null=True, blank=True)
    receipt_copies = models.PositiveSmallIntegerField("No of Receipt Copy", default=1)
    # From the client's feedback doc (row 60) via the wireframe. No legacy column.
    share_on_whatsapp = models.BooleanField("Share On WhatsApp", default=False)

    # The start of every order number printed at this station:
    # prefix + device registration number + six digits. BillContinuity takes a
    # copy when a device first counts here.
    order_no_prefix = models.CharField(
        "Order No Starting Characters", max_length=5, validators=[ORDER_NO_PREFIX]
    )

    # Test rides: how long a customer or a cashier may ride before returning.
    customer_test_minutes = models.PositiveSmallIntegerField(
        "Customer Test Time Slot (Min)", default=5
    )
    cashier_test_minutes = models.PositiveSmallIntegerField(
        "Cashier Test Time Slot (Min)", default=5
    )

    round_off_mode = models.CharField(
        "Round Off Type", max_length=16, choices=RoundOffMode.choices,
        default=RoundOffMode.UPWARD,
    )
    round_off_step = models.DecimalField(
        "Round Off Limit", max_digits=4, decimal_places=2,
        choices=ROUND_OFF_STEPS, default=Decimal("0.25"),
    )

    objects = DeviceSettingsManager()

    class Meta:
        db_table = "device_settings"
        verbose_name_plural = "device settings"
        ordering = ["branch__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch"],
                condition=Q(is_active=True),
                name="uniq_active_settings_per_branch",
                violation_error_message="This station already has active settings.",
            ),
            # Per company, not globally: two companies on one server can each
            # use "CF001". A receipt is still unique either way -- the device's
            # own registration id sits inside every printed number (section
            # 8.8), so nothing here is what stops two receipts from colliding.
            models.UniqueConstraint(
                fields=["company", "settings_code"],
                condition=Q(is_active=True),
                name="uniq_active_settings_code_per_company",
                violation_error_message="Settings code already exists for this company.",
            ),
            models.UniqueConstraint(
                fields=["company", "order_no_prefix"],
                condition=Q(is_active=True),
                name="uniq_active_order_no_prefix_per_company",
                violation_error_message="Another station of this company already uses this order prefix.",
            ),
            models.CheckConstraint(
                condition=Q(paper_feed__lte=10), name="device_settings_paper_feed_0_to_10"
            ),
            models.CheckConstraint(
                condition=Q(receipt_copies__gte=1, receipt_copies__lte=10),
                name="device_settings_receipt_copies_1_to_10",
            ),
            models.CheckConstraint(
                condition=Q(customer_test_minutes__gt=0, cashier_test_minutes__gt=0),
                name="device_settings_test_minutes_positive",
            ),
        ]
        indexes = [models.Index(fields=["branch"]), models.Index(fields=["company"])]

    def __str__(self):
        return f"{self.settings_code} — {self.branch}"

    def save(self, *args, **kwargs):
        # The legacy upper-cased the prefix when it built each order number
        # (ERP.Interface/DMS.cs:319); storing it upper-case means the uniqueness
        # rule sees "dubpp" and "DUBPP" as the same prefix.
        self.order_no_prefix = (self.order_no_prefix or "").upper()
        # Always the branch's own company -- never set by hand, so it cannot
        # drift even before the composite foreign key is checked at commit.
        if self.branch_id:
            self.company_id = self.branch.company_id
        super().save(*args, **kwargs)


class BillKind(models.TextChoices):
    """Legacy BillType: 2 = order, 3 = test ride. Hard-coded literals there; there
    was never an enum."""

    ORDER = "order", "Rental order"
    TEST_RIDE = "test_ride", "Test ride"


# The running number is zero-padded to six digits, as on every legacy receipt
# (Save_Order_Booking.sql:234 takes the last six characters back out).
BILL_NUMBER_WIDTH = 6


class BillContinuity(TimeStampedModel):
    """Where a device's receipt numbering stands, at one station, for one kind of bill.

    Replaces SfaBillContinuity. A tablet numbers its receipts itself, offline:
    it is told the last number at login, counts up, and reports back. What keeps
    two tablets at one station from printing the same number is not a reserved
    range -- the legacy never had one -- but the device's own number inside
    every receipt: prefix + device registration id + six digits.

    One row per (device, branch, kind). Moving a device to another station
    starts a new row at 0 there and leaves the old one as it was, which is what
    the legacy did; it is safe because the prefix differs per station.

    `prefix` is copied from the station's DeviceSettings when the row is first
    created, and kept. If the station's prefix is changed later, this device
    keeps printing in the format it started with, so its past and future numbers
    cannot collide.

    `last_number` is the last number *used*. It only ever moves forward, and it
    moves by being raised to what a device reports -- GREATEST(current, n) --
    never by a blind +1. The legacy's +1 per uploaded order counted a retried
    upload twice, and needed an IsLogin flag to stop late uploads counting at
    all; raising to the reported number makes both cases correct and the flag
    unnecessary.

    Dropped from the legacy table, on what its code did: SalesmanID and FormID
    were always written as 0, ToDate was never written, FromDate only fed a check
    that is always true for a row made at login, and IsApproved / IsActive were
    set to 1 and never changed.
    """

    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name="bill_counters")
    branch = models.ForeignKey(
        "company.Branch", on_delete=models.PROTECT, related_name="bill_counters"
    )
    kind = models.CharField(max_length=16, choices=BillKind.choices)
    prefix = models.CharField(max_length=12)
    # PositiveBigIntegerField: Postgres gets a ">= 0" check with it.
    last_number = models.PositiveBigIntegerField(default=0)

    class Meta:
        db_table = "bill_continuity"
        verbose_name_plural = "bill continuity"
        ordering = ["device", "branch", "kind"]
        constraints = [
            # The legacy had no such rule, and its QA data already holds two
            # duplicate groups -- each of which would break its
            # `SET @x = (SELECT BillNumber ...)` with "more than one value".
            models.UniqueConstraint(
                fields=["device", "branch", "kind"],
                name="uniq_bill_counter_per_device_branch_kind",
            ),
        ]
        indexes = [models.Index(fields=["branch"])]

    def __str__(self):
        return f"{self.device} at {self.branch}: {self.get_kind_display()} {self.last_number}"

    @property
    def next_number(self):
        return self.last_number + 1
