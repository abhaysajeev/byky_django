"""Request-level plumbing: who is signed in, and in which timezone.

This project does not install django.contrib.auth, so the usual
AuthenticationMiddleware is replaced by the one below. It reads the user id the
web sign-in put in the session and, crucially, checks that the AppSession row is
still open -- which is what makes a force logout take effect on the web
immediately (design/03-login.md section 5.1).
"""

import zoneinfo
from functools import partial

from django.utils import timezone
from django.utils.functional import SimpleLazyObject

SESSION_USER_KEY = "user_id"
SESSION_ID_KEY = "app_session_id"


def _load_user(request):
    from apps.portal.session_models import AppSession
    from core.models import User

    user_id = request.session.get(SESSION_USER_KEY)
    session_id = request.session.get(SESSION_ID_KEY)
    if not user_id or not session_id:
        return None

    session = (
        AppSession.objects
        .filter(pk=session_id, user_id=user_id, logged_out_at__isnull=True)
        .first()
    )
    if session is None:
        request.session.flush()
        return None

    user = User.objects.filter(pk=user_id, is_active=True).select_related("company", "role").first()
    if user is None:
        request.session.flush()
    return user


class CurrentUserMiddleware:
    """Puts `request.user` (a User or None) and `request.app_session_id` on
    every request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.user = SimpleLazyObject(partial(_load_user, request))
        request.app_session_id = request.session.get(SESSION_ID_KEY)
        return self.get_response(request)


class CompanyTimezoneMiddleware:
    """Renders every page in the signed-in user's company timezone.

    Storage stays UTC; this only affects display (design/00-findings.md
    section 7). One company, one timezone -- Branch carries none.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        company = getattr(getattr(request, "user", None), "company", None)
        name = getattr(company, "timezone", None)
        if name:
            try:
                timezone.activate(zoneinfo.ZoneInfo(name))
            except zoneinfo.ZoneInfoNotFoundError:
                timezone.deactivate()
        else:
            timezone.deactivate()
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
