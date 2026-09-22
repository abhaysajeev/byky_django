"""Permission rules.

One check, used by the web views and the app APIs alike (design/rbac.md
section 5). The legacy asked eight privilege tables and matched pages by
`PageUrl.Contains()`; this asks one table and matches by code.
"""

from apps.portal.models import Page, RolePermission


def has_permission(user, page_code, action):
    """Can this user do `action` on the screen `page_code`?

    There is no bypass: a user with no role has no permissions, and an action
    the screen does not offer is never granted.
    """
    role_id = getattr(user, "role_id", None)
    if not role_id or not user.is_active:
        return False

    return RolePermission.objects.filter(
        role_id=role_id,
        page__code=page_code,
        page__is_active=True,
        page__actions__contains=[action],
        **{f"can_{action}": True},
    ).exists()


def permissions_for(user, channel):
    """Everything the user may do on one app, for the login response and the
    sidebar. Only pages that app actually shows."""
    role_id = getattr(user, "role_id", None)
    if not role_id:
        return []

    rows = (
        RolePermission.objects.filter(
            role_id=role_id, page__is_active=True, page__channels__contains=[channel]
        )
        .select_related("page", "page__module")
        .order_by("page__module__sort_order", "page__sort_order")
    )
    return [
        {
            "page": row.page.code,
            "name": row.page.name,
            "url_name": row.page.url_name,
            "module": row.page.module.code,
            **{
                action: getattr(row, f"can_{action}")
                for action in row.page.actions
            },
        }
        for row in rows
    ]


def add_role_to_module(role, module):
    """Give a role a module: every screen listed, every box unticked.

    Adding a module never grants anything; the admin then ticks what the role
    may do (design/rbac.md section 6).
    """
    for page in Page.objects.filter(module=module, is_active=True):
        RolePermission.objects.get_or_create(role=role, page=page)


def remove_role_from_module(role, module):
    """Take a module away from a role. Scoped to that module, so the role's
    other modules are untouched."""
    RolePermission.objects.filter(role=role, page__module=module).delete()


def grant_all(role):
    """Tick every action each active page offers, for one role.

    Used when bootstrapping the first administrator and by
    `manage.py sync_role_pages` after new pages ship. Idempotent.
    """
    from apps.portal.models import RolePermission

    granted = 0
    for page in Page.objects.filter(is_active=True):
        permission, _ = RolePermission.objects.get_or_create(role=role, page=page)
        for action in page.actions:
            setattr(permission, f"can_{action}", True)
        permission.save()
        granted += 1
    return granted
