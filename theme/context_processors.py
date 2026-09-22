"""Context every page needs: the sidebar, and the navbar's alert feed."""

from theme.menu import build_menu


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
