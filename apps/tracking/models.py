"""Antenna Tracking: which vehicles a station's RFID reader hears, and what
that means (design/07-tracking.md).

New tables only. The rest of the system is read, never changed: a vehicle's
tag is Vehicle.rfid_epc, rentals / test rides / warehouse service come from
their own apps.

Volume lives in two tables: `tracking_reading` (raw reads, daily partitions,
dropped after the company's retention -- design §4) and `tracking_reading_key`
(the dedupe key, pruned with it). Everything the engine and the screens read
is small: about one row per tag, per vehicle, per reader.
"""

from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel


class ReaderStatus(models.TextChoices):
    PENDING = "pending", "Pending"      # connected, not known yet: its data is refused
    APPROVED = "approved", "Approved"
    BLOCKED = "blocked", "Blocked"      # the gateway closes its connections


class Direction(models.TextChoices):
    EAST = "E", "East"
    SOUTH = "S", "South"
    WEST = "W", "West"
    NORTH = "N", "North"


class Reader(TimeStampedModel):
    """One Marktrace reader ("mother", legacy RmsCPUDetails), set up by hand on the
    Readers screen (D8). The gateway inserts a pending row when an unknown
    reader connects."""

    company = models.ForeignKey("company.Company", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="+")       # set on approval
    device_id = models.CharField(max_length=16, unique=True)   # the reader's id, e.g. 190619101016089
    name = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=10, choices=ReaderStatus.choices, default=ReaderStatus.PENDING)
    enabled = models.BooleanField(default=False)   # its readings count only when on
    firmware = models.CharField(max_length=16, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    last_data_at = models.DateTimeField(null=True, blank=True)
    # measured from its heartbeats (P0); the engine's offline rule uses settings
    heartbeat_seconds = models.PositiveIntegerField(null=True, blank=True)
    # the engine's own view, so reader offline / online is an event only on a change
    online = models.BooleanField(db_default=False)
    online_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "tracking_reader"
        ordering = ["name", "device_id"]
        constraints = [
            models.CheckConstraint(condition=~Q(status=ReaderStatus.APPROVED) | Q(company__isnull=False),
                                   name="tracking_reader_approved_has_company"),
        ]

    def __str__(self):
        return self.name or self.device_id


class ReaderAssignment(TimeStampedModel):
    """Which station a reader stands at, from-to (legacy RmsCPURegistration)."""

    reader = models.ForeignKey(Reader, on_delete=models.PROTECT, related_name="assignments")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="+")
    from_at = models.DateTimeField()
    to_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "tracking_reader_assignment"
        ordering = ["-from_at"]
        constraints = [
            models.UniqueConstraint(fields=["reader"], condition=Q(to_at__isnull=True),
                                    name="tracking_one_open_assignment_per_reader"),
        ]


class Antenna(TimeStampedModel):
    """A reader's port 1-4 and its calibrated limits (design §6). No limits =
    any read counts as present (the legacy's only behaviour, E6)."""

    reader = models.ForeignKey(Reader, on_delete=models.CASCADE, related_name="antennas")
    port = models.PositiveSmallIntegerField()
    direction = models.CharField(max_length=1, choices=Direction.choices, blank=True)
    enter_rssi = models.SmallIntegerField(null=True, blank=True)   # dBm: at or above = clearly inside
    leave_rssi = models.SmallIntegerField(null=True, blank=True)   # dBm: below = outside
    baseline_rssi = models.SmallIntegerField(null=True, blank=True)  # parked median at approval (drift check)
    calibration = models.ForeignKey("tracking.Calibration", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    drift_note = models.CharField(max_length=200, blank=True)   # the daily drift check's warning (design §6)

    class Meta:
        db_table = "tracking_antenna"
        ordering = ["reader", "port"]
        constraints = [
            models.UniqueConstraint(fields=["reader", "port"], name="tracking_antenna_port_per_reader"),
            models.CheckConstraint(condition=Q(port__gte=1, port__lte=4), name="tracking_antenna_port_1_4"),
            models.CheckConstraint(condition=Q(enter_rssi__isnull=True) | Q(leave_rssi__isnull=True)
                                   | Q(enter_rssi__gt=models.F("leave_rssi")),
                                   name="tracking_antenna_enter_above_leave"),
        ]


class CalibrationMethod(models.TextChoices):
    FULL = "full", "Full survey"
    QUICK = "quick", "Quick (boundary + outside spots)"


class CalibrationStatus(models.TextChoices):
    OPEN = "open", "In progress"
    PROPOSED = "proposed", "Proposed"
    APPROVED = "approved", "Approved"
    DISCARDED = "discarded", "Discarded"


class Calibration(TimeStampedModel):
    """One calibration session of a reader (design §6)."""

    reader = models.ForeignKey(Reader, on_delete=models.PROTECT, related_name="calibrations")
    method = models.CharField(max_length=10, choices=CalibrationMethod.choices)
    status = models.CharField(max_length=10, choices=CalibrationStatus.choices, default=CalibrationStatus.OPEN)
    tags = models.JSONField(default=list)          # the calibration bikes' tags, 8-hex
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict)        # per port: counts, medians, proposed limits, overlap notes
    approved_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    check_until = models.DateTimeField(null=True, blank=True)   # the 3-day auto-check after approval
    check_result = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "tracking_calibration"
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(fields=["reader"], condition=Q(status__in=["open", "proposed"]),
                                    name="tracking_one_open_calibration_per_reader"),
        ]


class PointZone(models.TextChoices):
    INSIDE = "inside", "Inside"
    BOUNDARY = "boundary", "Boundary"
    OUTSIDE = "outside", "Outside"


class CalibrationPoint(models.Model):
    """One stop of a calibration walk: readings of the session's tags between
    started_at and ended_at belong to it."""

    calibration = models.ForeignKey(Calibration, on_delete=models.CASCADE, related_name="points")
    seq = models.PositiveSmallIntegerField()
    label = models.CharField(max_length=60)          # "Corner NE"
    zone = models.CharField(max_length=10, choices=PointZone.choices)
    variant = models.CharField(max_length=30, blank=True)   # "turned", "person standing"
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "tracking_calibration_point"
        ordering = ["calibration", "seq"]
        constraints = [models.UniqueConstraint(fields=["calibration", "seq"], name="tracking_point_seq")]

    def __str__(self):
        return f"{self.calibration_id}#{self.seq} {self.label}"


class Reading(models.Model):
    """One raw read. The table is partitioned by day on received_at and made by
    RunSQL (migration 0001): Django does not manage it, the gateway writes it,
    the screens read it by tag/reader and time."""

    pk = models.CompositePrimaryKey("reader_id", "port", "tag", "received_at")
    reader = models.ForeignKey(Reader, on_delete=models.DO_NOTHING, db_constraint=False, related_name="+")
    port = models.SmallIntegerField()
    tag = models.CharField(max_length=8)
    reader_time = models.DateTimeField(null=True)   # naive, as the reader sent it (UAE time)
    received_at = models.DateTimeField()           # server time -- the one that counts (E11)
    rssi = models.SmallIntegerField()
    entry = models.BooleanField()
    staying = models.BooleanField()
    low_battery = models.BooleanField()

    class Meta:
        managed = False
        db_table = "tracking_reading"

    def __str__(self):
        return f"{self.tag} @ {self.received_at:%Y-%m-%d %H:%M:%S}"


class ReadingKey(models.Model):
    """The dedupe key of a read (design §4): a reader re-sends what we did not
    ACK (E8), and a partitioned table's unique index would have to include
    received_at. ON CONFLICT DO NOTHING here decides which reads are new."""

    pk = models.CompositePrimaryKey("reader_id", "port", "tag", "reader_time")
    reader = models.ForeignKey(Reader, on_delete=models.CASCADE, related_name="+")
    port = models.SmallIntegerField()
    tag = models.CharField(max_length=8)
    reader_time = models.DateTimeField()
    received_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "tracking_reading_key"

    def __str__(self):
        return f"{self.tag} {self.reader_time}"


class RawFrame(models.Model):
    """Frames as received, for the P0 capture and for debugging a reader."""

    device_id = models.CharField(max_length=16, blank=True)
    peer = models.CharField(max_length=64, blank=True)
    received_at = models.DateTimeField(db_index=True)
    data = models.BinaryField()
    crc_ok = models.BooleanField()
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "tracking_raw_frame"
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.device_id} {self.received_at:%Y-%m-%d %H:%M:%S}"


class TagLastSeen(models.Model):
    """The newest *new* read of a tag (design §5: a replayed or late batch never
    moves seen_at)."""

    tag = models.CharField(max_length=8, primary_key=True)
    reader = models.ForeignKey(Reader, on_delete=models.CASCADE, related_name="+")
    port = models.SmallIntegerField()
    seen_at = models.DateTimeField()
    reader_time = models.DateTimeField(null=True)
    rssi = models.SmallIntegerField()
    rssi_avg = models.FloatField()                # EWMA, per tag
    low_battery = models.BooleanField(default=False)

    class Meta:
        db_table = "tracking_tag_last_seen"

    def __str__(self):
        return self.tag


class PresenceState(models.TextChoices):
    PRESENT = "present", "Present"
    ALERT = "alert", "At edge"
    MISSING = "missing", "Missing"
    ON_RENT = "on_rent", "On rent"
    ON_TEST = "on_test", "On test"
    IN_SERVICE = "in_service", "In service"
    UNKNOWN = "unknown", "Unknown"


class VehiclePresence(models.Model):
    """Where each tagged vehicle stands now -- one row per vehicle, written by the
    engine only."""

    vehicle = models.OneToOneField("fleet.Vehicle", primary_key=True, on_delete=models.CASCADE, related_name="+")
    company = models.ForeignKey("company.Company", on_delete=models.CASCADE, related_name="+")
    branch = models.ForeignKey("company.Branch", null=True, on_delete=models.SET_NULL, related_name="+")
    state = models.CharField(max_length=12, choices=PresenceState.choices)
    reason = models.CharField(max_length=30, blank=True)   # e.g. test_overdue, reader_offline, no_reader
    since = models.DateTimeField()
    last_seen_at = models.DateTimeField(null=True)
    heard_by_branch = models.ForeignKey("company.Branch", null=True, on_delete=models.SET_NULL, related_name="+")
    low_battery = models.BooleanField(default=False)
    # where a tag's silence is counted from when that is later than last_seen_at:
    # the moment it stopped being excused (back from rent, reader back online)
    clock_from = models.DateTimeField(null=True)

    class Meta:
        db_table = "tracking_vehicle_presence"
        indexes = [models.Index(fields=["company", "branch", "state"], name="tracking_presence_board")]

    def __str__(self):
        return f"{self.vehicle_id}: {self.state}"


class EventKind(models.TextChoices):
    STATE = "state", "State change"
    LOW_BATTERY = "low_battery", "Low battery"
    READER_OFFLINE = "reader_offline", "Reader offline"
    READER_ONLINE = "reader_online", "Reader back online"


class PresenceEvent(models.Model):
    """The history the bell and Missing & Alerts read. Never edited, except to
    acknowledge it."""

    company = models.ForeignKey("company.Company", on_delete=models.CASCADE, related_name="+")
    branch = models.ForeignKey("company.Branch", null=True, on_delete=models.SET_NULL, related_name="+")
    vehicle = models.ForeignKey("fleet.Vehicle", null=True, on_delete=models.CASCADE, related_name="+")
    reader = models.ForeignKey(Reader, null=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=16, choices=EventKind.choices, default=EventKind.STATE)
    from_state = models.CharField(max_length=12, choices=PresenceState.choices, blank=True)
    to_state = models.CharField(max_length=12, choices=PresenceState.choices, blank=True)
    reason = models.CharField(max_length=30, blank=True)
    at = models.DateTimeField()
    alerts = models.BooleanField(default=False)    # shown in the bell (missing/edge/low battery/offline, alerts on)
    acknowledged_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="+")
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "tracking_presence_event"
        ordering = ["-at"]
        indexes = [
            models.Index(fields=["company", "-at"], name="tracking_event_company_at"),
            models.Index(fields=["vehicle", "-at"], name="tracking_event_vehicle_at"),
        ]

    def __str__(self):
        return f"{self.kind} {self.to_state} {self.at:%Y-%m-%d %H:%M}"


class TrackingSettings(models.Model):
    """Per company, edited on the Tracking Settings page (D2, D6)."""

    company = models.OneToOneField("company.Company", primary_key=True, on_delete=models.CASCADE, related_name="+")
    alert_after_s = models.PositiveIntegerField(default=150)
    missing_after_s = models.PositiveIntegerField(default=300)
    offline_after_s = models.PositiveIntegerField(default=300)
    retention_days = models.PositiveSmallIntegerField(default=3)
    allowance_in_db = models.PositiveSmallIntegerField(default=3)
    allowance_out_db = models.PositiveSmallIntegerField(default=3)
    modified_on = models.DateTimeField(auto_now=True)
    modified_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "tracking_settings"
        constraints = [
            models.CheckConstraint(condition=Q(alert_after_s__lt=models.F("missing_after_s")),
                                   name="tracking_alert_before_missing"),
            models.CheckConstraint(condition=Q(retention_days__gte=1, retention_days__lte=14),
                                   name="tracking_retention_1_14"),
        ]

    def __str__(self):
        return f"Tracking settings of {self.company_id}"


class BranchTrackingSettings(models.Model):
    """One station's row of Tracking Settings (owner, 9 Nov 2026): alerts on or
    off, and optional timings. A blank timing is the company default."""

    branch = models.OneToOneField("company.Branch", primary_key=True, on_delete=models.CASCADE, related_name="+")
    alerts_on = models.BooleanField(default=False)
    alert_after_s = models.PositiveIntegerField(null=True, blank=True)
    missing_after_s = models.PositiveIntegerField(null=True, blank=True)
    offline_after_s = models.PositiveIntegerField(null=True, blank=True)
    modified_on = models.DateTimeField(auto_now=True)
    modified_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "tracking_branch_settings"

    def __str__(self):
        return f"Tracking settings of branch {self.branch_id}"


class AlertRead(models.Model):
    """The web bell's read marker, per user (design §7.1)."""

    user = models.OneToOneField("core.User", primary_key=True, on_delete=models.CASCADE, related_name="+")
    last_read_at = models.DateTimeField()

    class Meta:
        db_table = "tracking_alert_read"

    def __str__(self):
        return f"{self.user_id} read to {self.last_read_at}"
