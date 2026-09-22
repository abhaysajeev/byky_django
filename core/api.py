"""What every app-facing API shares: the URL's app, the envelope, the errors.

The contract is design/registration/registration-api.md sections 4-5:

    request   {"credentials": {...}, "request_data": {...}}
    response  {"code": "...", "message": "...", "data": {...}}

`credentials` is the block the apps send on every call. It is accepted whole
and never validated; an endpoint reads a key from it only when a feature needs
that key. `request_data` is the endpoint's actual input and is type-checked by
that endpoint's serializer.

Every reply -- success, a field error, a throttle, an unknown URL, a crash --
comes back in the envelope, so the app never has to parse an HTML error page.
"""

import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import exceptions, serializers, status
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView, set_rollback
from rest_framework.views import exception_handler as drf_exception_handler

from core.enums import Channel

log = logging.getLogger(__name__)

APPS = (Channel.OPERATOR, Channel.MANAGER, Channel.EMPLOYEE)


class AppConverter:
    """`{app}` in /api/v1/{app}/...: one of the three phone apps.

    `web` is a channel too, but it has no API -- it signs in through the
    screens. Anything else never reaches a view.
    """

    regex = "|".join(APPS)

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


def envelope(code, message, data=None, http_status=status.HTTP_200_OK, headers=None):
    return Response(
        {"code": code, "message": message, "data": data or {}},
        status=http_status, headers=headers,
    )


def request_parts(request):
    """(credentials, request_data) from the body.

    A missing block counts as empty, so a missing `request_data` is reported
    field by field by the endpoint's serializer. A block that is not an object
    is a bug in the app and is reported as one; a malformed `credentials` is
    ignored like everything else in it.
    """
    body = request.data
    if not isinstance(body, dict):
        raise exceptions.ValidationError({"body": ["must be a JSON object"]})
    credentials = body.get("credentials")
    request_data = body.get("request_data", {})
    if not isinstance(request_data, dict):
        raise exceptions.ValidationError({"request_data": ["must be an object"]})
    return (credentials if isinstance(credentials, dict) else {}), request_data


class PublicAPIView(APIView):
    """Base for calls made before login: update check, registration, login.

    The project default is JWT + IsAuthenticated. Left in place it would answer
    401 to every tablet on its first call, which has no token to send -- so both
    are switched off here, explicitly.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [JSONParser]


def _first_message(detail):
    """The first human message in a DRF error detail, however nested."""
    if isinstance(detail, dict):
        return _first_message(next(iter(detail.values()), ""))
    if isinstance(detail, list):
        return _first_message(detail[0]) if detail else ""
    return str(detail)


# Codes the app switches on, by the DRF exception that produced them.
_CODES = {
    exceptions.ParseError: ("invalid_request", "Body is not valid JSON."),
    exceptions.UnsupportedMediaType: ("unsupported_media_type", "Send application/json."),
    exceptions.MethodNotAllowed: ("method_not_allowed", "Method not allowed."),
    exceptions.NotFound: ("not_found", "Not found."),
    exceptions.Throttled: ("rate_limited", "Too many requests. Try again later."),
    exceptions.NotAuthenticated: ("not_authenticated", "Sign in first."),
    exceptions.AuthenticationFailed: ("not_authenticated", "Sign in first."),
    exceptions.PermissionDenied: ("forbidden", "Not allowed."),
}


def exception_handler(exc, context):
    """REST_FRAMEWORK["EXCEPTION_HANDLER"]: every DRF error in the envelope.

    Only DRF views reach this, so the web screens are unaffected.
    """
    response = drf_exception_handler(exc, context)

    if response is None:
        # Not an APIException: a bug or an outage. Logged with its traceback,
        # answered without one.
        log.exception("unhandled error in %s", context.get("view").__class__.__name__)
        set_rollback()
        return envelope(
            "server_error", "Something went wrong. Try again.",
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, exceptions.ValidationError):
        errors = exc.detail if isinstance(exc.detail, dict) else {"non_field_errors": exc.detail}
        errors = {field: _first_message(msgs) for field, msgs in errors.items()}
        field, text = next(iter(errors.items()))
        message = f"{text}." if field == "non_field_errors" else f"{field} {text}."
        return envelope(
            "invalid_request", message, {"errors": errors},
            http_status=response.status_code,
        )

    code, message = next(
        (pair for cls, pair in _CODES.items() if isinstance(exc, cls)),
        (exc.default_code, _first_message(exc.detail)),
    )
    # DRF's own headers carry Retry-After on a 429 and Allow on a 405.
    headers = {k: v for k, v in response.items() if k in ("Retry-After", "Allow")}
    return envelope(code, message, http_status=response.status_code, headers=headers)


@csrf_exempt
def not_found(request, *args, **kwargs):
    """Any /api/ URL that matched nothing -- an unknown app such as `web`,
    or a typo -- answers in the envelope instead of Django's HTML 404."""
    return JsonResponse(
        {"code": "not_found", "message": "Not found.", "data": {}},
        status=status.HTTP_404_NOT_FOUND,
    )


# -- Field messages -----------------------------------------------------------
#
# Short and lower-case, because the envelope's message reads
# "<field> <text>." -- "version_code must be a whole number."

REQUIRED = {"required": "is required", "null": "is required", "blank": "is required"}


def text_field(max_length, **kwargs):
    return serializers.CharField(
        max_length=max_length,
        error_messages={
            **REQUIRED,
            "invalid": "must be text",
            "max_length": f"must be at most {max_length} characters",
        },
        **kwargs,
    )


def whole_number_field(**kwargs):
    return serializers.IntegerField(
        min_value=0,
        error_messages={
            **REQUIRED,
            "invalid": "must be a whole number",
            "max_string_length": "must be a whole number",
            "min_value": "must be 0 or more",
        },
        **kwargs,
    )
