"""Values shared by every module."""

from django.db import models


class Channel(models.TextChoices):
    """Where a person signs in. See design/03-login.md."""

    WEB = "web", "Back office web"
    OPERATOR = "operator", "RMS operator app"
    MANAGER = "manager", "Manager app"
    EMPLOYEE = "employee", "Employee app"


class ApprovalStatus(models.IntegerChoices):
    """The approval pattern from design/00-findings.md section 5."""

    PENDING = 0, "Pending"
    APPROVED = 1, "Approved"
    REJECTED = 2, "Rejected"


class UserScope(models.TextChoices):
    """How much of the system a user can see.

    Deliberately an explicit field rather than "company is empty": a user
    created without a company by mistake must see nothing, not everything.
    Absence of data should never grant power.
    """

    COMPANY = "company", "One company"
    SYSTEM = "system", "Every company"
