"""Template tags the shell uses.

Ported from the wireframe, minus every filter that called
django.contrib.auth (has_group, is_admin, is_staff, is_superuser and their
*_required partners) -- this project has no auth app and its own permission
model (design/rbac.md).

Registered in TEMPLATES["OPTIONS"]["builtins"], which is why the sidebar
templates use these filters with no {% load %}.
"""

from django import template
from django.utils.safestring import mark_safe

from theme.template_helpers import TemplateHelper

register = template.Library()


@register.simple_tag
def get_theme_variables(scope):
    return mark_safe(TemplateHelper.get_theme_variables(scope))


@register.simple_tag
def get_theme_config(scope):
    return mark_safe(TemplateHelper.get_theme_config(scope))


@register.simple_tag(takes_context=True)
def current_url(context):
    request = context["request"]
    return request.resolver_match.url_name if request.resolver_match else ""


@register.filter
def filter_by_url(submenu, url):
    """True when any item in this group is the page being shown, so the group
    renders open and active.

    `resolver_match` is None while rendering an error page, hence the getattr --
    the wireframe's version raised AttributeError there.
    """
    if not submenu:
        return False
    current = getattr(url.resolver_match, "url_name", None)
    for subitem in submenu:
        subitem_url = subitem.get("url")
        if subitem_url == url.path or (current and subitem_url == current):
            return True
        if subitem.get("submenu") and filter_by_url(subitem["submenu"], url):
            return True
    return False


@register.filter
def has_permission(user, page_code):
    """Used by menu items that name a page code. Reads our own permission
    model, never django.contrib.auth."""
    from apps.portal.services import has_permission as check

    return bool(user) and check(user, page_code, "read")


@register.filter
def initials(name):
    """'System Administrator' -> 'SA', for the navbar avatar."""
    parts = [p for p in str(name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()
