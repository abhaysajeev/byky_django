"""Applying the page registry.

Shared by `manage.py sync_pages` and by the migration that runs it on a fresh
database, so a screen is described in exactly one place
(apps/portal/page_registry.py).

The migration deliberately re-asserts the **current** registry rather than a
snapshot of it. That is the opposite of the usual rule about data migrations,
and it is right here for one reason: Module and Page describe what the software
has, not what a customer has done. There is nothing to preserve a historical
version of -- a screen that no longer exists in this release should not be
recreated on a fresh install just because it shipped in an earlier one.

Nothing here grants anything. Access stays a separate, deliberate act
(design/rbac.md section 7).
"""

from apps.portal import page_registry as registry


def sync(apps=None):
    """Write the registry. Returns (created, updated, deactivated).

    Pass the migration's `apps` to use historical models; omit it in the
    command, where the real ones are fine.
    """
    if apps is None:
        from apps.portal.models import Module, Page
    else:
        Module = apps.get_model("portal", "Module")
        Page = apps.get_model("portal", "Page")

    created = updated = 0

    modules = {}
    for code, name, header, sort_order, is_flat, svg, svg2 in registry.MODULES:
        module, was_created = Module.objects.update_or_create(
            code=code,
            defaults={
                "name": name, "menu_header": header, "sort_order": sort_order,
                "is_flat": is_flat, "svg": svg, "svg2": svg2, "is_active": True,
            },
        )
        modules[code] = module
        created, updated = created + was_created, updated + (not was_created)

    for code, module_code, name, url_name, actions, sort_order, is_retired in registry.PAGES:
        svg, svg2 = registry.PAGE_ICONS.get(code, ("", ""))
        _, was_created = Page.objects.update_or_create(
            code=code,
            defaults={
                "module": modules[module_code],
                "name": name,
                "url_name": url_name,
                "actions": list(actions),
                "channels": ["web"],
                "sort_order": sort_order,
                "svg": svg,
                "svg2": svg2,
                # A retired screen keeps its row so its RolePermission history
                # survives, but it is off: no sidebar entry, and
                # has_permission() refuses it.
                "is_active": not is_retired,
            },
        )
        created, updated = created + was_created, updated + (not was_created)

    # A page that has dropped out of the registry entirely is deactivated, never
    # deleted: the rows saying who could open it are the record of that.
    stale = list(
        Page.objects.filter(is_active=True)
        .exclude(code__in=[row[0] for row in registry.PAGES])
        .values_list("code", flat=True)
    )
    if stale:
        Page.objects.filter(code__in=stale).update(is_active=False)

    return created, updated, stale
