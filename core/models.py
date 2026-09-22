"""Shared base models and the user identity.

The user lives here because every module references it through
settings.AUTH_USER_MODEL. Its links to company, role and employee are added by
migration as those modules are built (design/03-login.md section 8.1).
"""

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.postgres.fields import ArrayField
from django.db import models

from core.enums import ApprovalStatus, Channel, UserScope


class TimeStampedModel(models.Model):
    """Created/modified stamps. The legacy carried this quadruple on virtually
    every table; here it comes from one place."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    created_on = models.DateTimeField(auto_now_add=True)
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    modified_on = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        abstract = True


class ApprovalMixin(models.Model):
    """Master tables carry these three together; log and assignment tables
    carry none of them."""

    want_approval = models.BooleanField(default=False)
    approval_status = models.PositiveSmallIntegerField(
        choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True

    def apply_approval_defaults(self):
        """Wanting approval means pending and inactive until someone approves;
        not wanting it means approved and active straight away."""
        if self.want_approval:
            self.approval_status = ApprovalStatus.PENDING
            self.is_active = False
        else:
            self.approval_status = ApprovalStatus.APPROVED
            self.is_active = True


class UserManager(BaseUserManager):
    def create_user(self, username, password=None, **extra):
        if not username:
            raise ValueError("A user needs a username.")
        user = self.model(username=self.model.normalize_username(username), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user


class User(AbstractBaseUser):
    """One identity for the web and all three apps.

    Deliberately not PermissionsMixin: permissions come from Role and
    RolePermission (design/rbac.md), not from django.contrib.auth.
    """

    username = models.CharField(max_length=100, unique=True)
    display_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    mobile = models.CharField(max_length=20, blank=True)

    # A company user belongs to exactly one company and sees only its rows.
    # A system user (BYKY's own staff) belongs to none and sees every company.
    # The pair is held together by constraints below, so neither can be set
    # without the other agreeing.
    scope = models.CharField(
        max_length=16, choices=UserScope.choices, default=UserScope.COMPANY
    )
    company = models.ForeignKey(
        "company.Company", null=True, blank=True,
        on_delete=models.PROTECT, related_name="users",
    )

    role = models.ForeignKey(
        "portal.Role", null=True, blank=True,
        on_delete=models.PROTECT, related_name="users",
    )  # one role per user, from the same company

    # Set for app users; empty for a back-office account with no staff record.
    # A blocked employee cannot sign in (design/03-login.md section 4).
    employee = models.OneToOneField(
        "crew.Employee", null=True, blank=True,
        on_delete=models.PROTECT, related_name="user",
    )

    allowed_channels = ArrayField(
        models.CharField(max_length=16, choices=Channel.choices),
        default=list, blank=True,
    )

    # Lockout: 15 failed attempts, 5 minutes (design/03-login.md decision 4).
    failed_login_count = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    want_approval = models.BooleanField(default=False)
    approval_status = models.PositiveSmallIntegerField(
        choices=ApprovalStatus.choices, default=ApprovalStatus.APPROVED
    )
    is_active = models.BooleanField(default=True)

    created_on = models.DateTimeField(auto_now_add=True)
    modified_on = models.DateTimeField(auto_now=True, null=True)

    objects = UserManager()
    USERNAME_FIELD = "username"

    class Meta:
        db_table = "user"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(scope="company", company__isnull=False)
                    | models.Q(scope="system", company__isnull=True)
                ),
                name="user_scope_matches_company",
            ),
        ]

    def __str__(self):
        return self.display_name or self.username

    @classmethod
    def normalize_username(cls, username):
        """Usernames are compared without case, so 'Sara.K' and 'sara.k' are one
        login (design/03-login.md section 3)."""
        return (username or "").strip().lower()

    def save(self, *args, **kwargs):
        self.username = self.normalize_username(self.username)
        super().save(*args, **kwargs)

    def can_use(self, channel):
        return channel in (self.allowed_channels or [])

    @property
    def sees_every_company(self):
        return self.scope == UserScope.SYSTEM
