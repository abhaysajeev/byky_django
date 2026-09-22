"""The sidebar, built from the database.

The wireframe read a static `vertical_menu.json`, so its menu and its
permissions were unrelated lists that had to be kept in step by hand. Here the
menu *is* the role's readable pages, which is what design/rbac.md specifies: a
screen a role cannot open never appears.

The sidebar templates are unchanged, so this returns exactly the shape they
loop over:

    {"menu": [
        {"url", "name", "slug", "svg", "svg2"},          # top-level link
        {"menu_header": "Operations"},                    # section divider
        {"name", "slug", "svg", "submenu": [{"url", "name", "slug"}]},
    ]}
"""

from itertools import groupby

from django.urls import NoReverseMatch, reverse

from apps.portal.models import Page
from apps.portal.services import permissions_for
from core.enums import Channel


def _resolves(url_name):
    """A menu item whose route does not exist would raise NoReverseMatch inside
    `{% url item.url %}` and take down the whole sidebar -- and with it every
    page. A Page row can be seeded before its screen is built, so this is a real
    case, not a theoretical one."""
    if not url_name:
        return False
    try:
        reverse(url_name)
    except NoReverseMatch:
        return False
    return True


def build_menu(user):
    if not user:
        return {"menu": []}

    readable = {
        row["page"] for row in permissions_for(user, Channel.WEB) if row.get("read")
    }
    if not readable:
        return {"menu": []}

    pages = (
        Page.objects.filter(code__in=readable, is_active=True, module__is_active=True)
        .select_related("module")
        .order_by("module__sort_order", "module__id", "sort_order", "name")
    )

    items = []
    headers_done = set()

    for module, group in groupby(pages, key=lambda page: page.module):
        group = [page for page in group if _resolves(page.url_name)]
        if not group:
            continue

        if module.menu_header and module.menu_header not in headers_done:
            items.append({"menu_header": module.menu_header})
            headers_done.add(module.menu_header)

        if module.is_flat:
            items.extend(
                {
                    "url": page.url_name,
                    "name": page.name,
                    "slug": page.url_name,
                    "svg": page.svg,
                    "svg2": page.svg2,
                }
                for page in group
            )
            continue

        items.append({
            "name": module.name,
            "slug": module.code,
            "svg": module.svg,
            "svg2": module.svg2,
            "submenu": [
                {"url": page.url_name, "name": page.name, "slug": page.url_name}
                for page in group
            ],
        })

    return {"menu": items}
