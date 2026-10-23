"""Context every page needs: the sidebar, the navbar's alert feed, and this
copy's CSRF cookie name."""

from django.conf import settings

from theme.menu import build_menu


def cookie_names(request):
    """The CSRF cookie's name, for scripts that read it (byky-crud.js): it is
    per copy (settings.COOKIE_PREFIX), so a script must not assume 'csrftoken'."""
    return {"csrf_cookie_name": settings.CSRF_COOKIE_NAME}


def sidebar_menu(request):
    """`menu_data` for templates/sidebar/sidebar.html.

    Built from the signed-in role's readable pages, so the menu and the
    permissions can never drift apart (design/rbac.md).
    """
    return {"menu_data": build_menu(getattr(request, "user", None))}


def alert_feed(request):
    """The navbar's bell. Empty until an alerts source exists; byky-alerts.js
    returns early on an empty feed."""
    return {"alert_feed": []}
