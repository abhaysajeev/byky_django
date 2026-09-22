"""Roles and privileges.

Built from design/rbac.md and design/rbac_schema.pdf. Replaces the legacy's
eight parallel Menu/Page/RolePrivileges table sets and its
`Page.PageUrl.Contains()` lookup, which was a prefix match that granted another
screen's rights on a URL collision.

Module and Page are shared by every company and maintained by developers.
Role and RolePermission belong to one company.
"""

from django.contrib.postgres.fields import ArrayField
from django.db import models

from core.enums import Channel
from core.models import ApprovalMixin, TimeStampedModel


class Action(models.TextChoices):
    CREATE = "create", "Create"
    READ = "read", "Read"
    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"
    PRINT = "print", "Print"
    RECOMMEND = "recommend", "Recommend"
    APPROVE = "approve", "Approve"


class Module(models.Model):
    """The SRS modules, as the sidebar groups them."""

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    # Sidebar presentation. The menu is built from these rows rather than from
    # a static JSON file (theme/menu.py).
    menu_header = models.CharField(max_length=50, blank=True)   # section divider above it
    is_flat = models.BooleanField(default=False)                # pages render as top-level links
    svg = models.CharField(max_length=1000, blank=True)         # icon path data
    svg2 = models.CharField(max_length=1000, blank=True)        # second path, when the icon needs one
    # Presentation. css_class names a class the stylesheet already defines --
    # never raw HTML, which would let anyone who can edit a module row inject
    # markup into every user's page. icon is a file on disk; only its path is
    # stored (see image_storage in DESIGN.md).
    css_class = models.CharField(max_length=100, blank=True)
    icon = models.ImageField(upload_to="menu/", blank=True)

    class Meta:
        db_table = "module"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Page(models.Model):
    """One row per screen. Tabs and drawers on a screen are covered by that
    screen's row. Identified by code, never by URL."""

    module = models.ForeignKey(Module, on_delete=models.PROTECT, related_name="pages")
    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=100)
    url_name = models.CharField(max_length=200, blank=True)

    # Which apps expose this screen, and which checkboxes the privilege matrix
    # shows for it.
    channels = ArrayField(
        models.CharField(max_length=16, choices=Channel.choices), default=list, blank=True
    )
    actions = ArrayField(
        models.CharField(max_length=16, choices=Action.choices), default=list, blank=True
    )

    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    # Only read when the page's module is flat, i.e. it draws as a top-level
    # sidebar link and therefore carries its own icon.
    svg = models.CharField(max_length=1000, blank=True)
    svg2 = models.CharField(max_length=1000, blank=True)
    css_class = models.CharField(max_length=100, blank=True)
    icon = models.ImageField(upload_to="menu/", blank=True)

    class Meta:
        db_table = "page"
        ordering = ["module", "sort_order", "name"]
        indexes = [models.Index(fields=["module"])]

    def __str__(self):
        return self.name


class Role(ApprovalMixin, TimeStampedModel):
    """A company's role, or -- when company is empty -- a system role held only
    by platform staff (core.enums.UserScope.SYSTEM)."""

    company = models.ForeignKey(
        "company.Company", null=True, blank=True,
        on_delete=models.PROTECT, related_name="roles",
    )
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "role"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="uniq_role_name_per_company"),
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(company__isnull=True),
                name="uniq_system_role_name",
            ),
        ]

    def __str__(self):
        return self.name


class RolePermission(TimeStampedModel):
    """The only table that grants access: one row per role and screen.

    A row with every flag False means the role has the screen listed in its
    module but can do nothing with it -- which is how a role starts
    (design/rbac.md section 6).
    """

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="permissions")
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="permissions")

    can_create = models.BooleanField(default=False)
    can_read = models.BooleanField(default=False)  # also covers "Access"
    can_update = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_print = models.BooleanField(default=False)
    # Recommend is the step before approve: one person puts a record forward,
    # another decides. Separate from approve so the two can never sit with the
    # same role by accident.
    can_recommend = models.BooleanField(default=False)
    can_approve = models.BooleanField(default=False)

    class Meta:
        db_table = "role_permission"
        constraints = [
            models.UniqueConstraint(fields=["role", "page"], name="uniq_role_page"),
        ]
        indexes = [models.Index(fields=["role"])]

    def __str__(self):
        return f"{self.role} / {self.page}"


# The session table lives in its own module for readability; Django needs it
# imported here so the app registry finds it.
from apps.portal.session_models import AppSession, LogoutReason  # noqa: E402,F401
