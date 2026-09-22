"""Authenticating an app's access token (design/03-login.md section 6.2/7.4).

The only authenticated app-facing endpoint today is /auth/logout; every other
app call in this project so far is public (update-check, registration, login,
refresh -- none of which have a session to check yet).

Deliberately not rest_framework_simplejwt.authentication.JWTAuthentication:
same reason as apps/portal/jwt.py -- its import chain needs
django.contrib.auth, which is not installed here.
"""

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from apps.portal import jwt as app_jwt
from apps.portal.session_models import AppSession


class AppJWTAuthentication(BaseAuthentication):
    """Bearer <access token> -> (user, session).

    Checks the AppSession row by the token's session id rather than trusting
    the access token alone (section 7.4's own reasoning for the upload
    endpoint) -- a force logout or a blocked employee closes the session, and
    that must take effect before the token's own 30-minute expiry would.
    """

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None

        try:
            payload = app_jwt.decode_token(header[len("Bearer "):], expected_type="access")
        except app_jwt.TokenInvalid:
            raise AuthenticationFailed("Invalid or expired token.")

        session = (
            AppSession.objects
            .filter(pk=payload.get("sid"), logged_out_at__isnull=True)
            .select_related("user")
            .first()
        )
        if session is None:
            raise AuthenticationFailed("Session closed.")
        if str(session.user_id) != payload.get("sub"):
            raise AuthenticationFailed("Invalid token.")
        # Same re-check core.middleware._load_user does on every web request
        # -- without it, deactivating a user account (outside the "HR blocks
        # an employee" path, which does close sessions) leaves their app
        # access token working until it naturally expires.
        if not session.user.is_active:
            raise AuthenticationFailed("Account not active.")

        return (session.user, session)

    def authenticate_header(self, request):
        # Without this, DRF's APIView.handle_exception downgrades a missing
        # or invalid token from 401 to 403 (no authenticator offered a
        # WWW-Authenticate challenge, so it assumes none *could* authenticate
        # and treats it as a permissions problem, not a login problem).
        return "Bearer"
