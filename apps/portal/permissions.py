"""Guarding a screen.

Both halves matter: the view refuses, and the template hides. The legacy only
hid buttons, which is why its permissions were never actually enforced.
"""

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from apps.portal.page_registry import MENU_URL
from apps.portal.services import has_permission


class PagePermissionMixin:
    """Set `page_code` and `required_action` on a view.

    A signed-out visitor is sent to the sign-in page; a signed-in user without
    the permission gets 403.
    """

    page_code = None
    required_action = "read"

    def dispatch(self, request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not user:
            return redirect(f"/login/?next={request.path}")

        if self.page_code and not has_permission(user, self.page_code, self.required_action):
            raise PermissionDenied("You do not have permission for this screen.")

        # The sidebar marks the page's own menu entry -- also on its detail,
        # add and edit screens, whose URL names the menu does not list.
        request.menu_url_name = MENU_URL.get(self.page_code)
        return super().dispatch(request, *args, **kwargs)
