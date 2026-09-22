"""drf-spectacular building blocks shared by every app-facing view.

Every endpoint under /api/v1/ replies in the {code, message, data} envelope
(core/api.py::envelope) and takes the {credentials, request_data} envelope on
the way in (core/api.py::request_parts). drf-spectacular has no built-in idea
of either shape -- it infers schema from serializers and view classes, and
these views use plain dicts, not serializers, for their responses. So each
view documents itself through the two helpers below, and every possible
reply (success included) is a `(status, code, message, data)` row transcribed
straight from that endpoint's design doc -- one list is the source for both
the Swagger page and the doc, instead of two things that can drift apart.
"""

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.utils import OpenApiExample, OpenApiResponse
from rest_framework import serializers


class EnvelopeSerializer(serializers.Serializer):
    """Schema-only -- the outer shape every reply has. `data`'s real shape
    changes with `code`, which DRF's serializer types can't express, so it
    stays a free-form object here; the actual shape is what the matching
    OpenApiExample in `envelope_responses()` shows."""

    code = serializers.CharField()
    message = serializers.CharField()
    data = serializers.DictField()


def envelope_responses(*rows):
    """Build a drf-spectacular `responses=` dict from `(status, code, message,
    data)` rows -- one row per possible reply, success included, in the same
    order the design doc's own refusal-code table lists them. A row may carry
    a 5th element, a display name, for the rare case where one `code` covers
    two different reply shapes on the same endpoint (`login-for-*.md`'s
    shared `ok` on one URL that behaves differently per app) -- omit it and
    the name defaults to `code`, which is right almost everywhere.

    Several codes commonly share one HTTP status (403 alone can be
    `company_inactive`, `user_not_approved`, `wrong_channel`, `user_blocked`,
    `device_blocked` or `device_retired` on a single endpoint) -- OpenAPI
    keys `responses` by status only, so same-status rows are grouped into one
    `OpenApiResponse` carrying several named examples; Swagger UI shows them
    as a dropdown under that status instead of overwriting each other.
    """
    by_status = {}
    for row in rows:
        status, code, message, data = row[:4]
        name = row[4] if len(row) > 4 else code
        by_status.setdefault(status, []).append(
            OpenApiExample(
                name, value={"code": code, "message": message, "data": data},
                status_codes=[str(status)],
            )
        )
    return {
        status: OpenApiResponse(response=EnvelopeSerializer, examples=examples)
        for status, examples in by_status.items()
    }


def envelope_request(name, request_data_serializer_class, *, request_data_required=True):
    """The one request shape every endpoint takes (core/api.py::request_parts):
    `{"credentials": {...}, "request_data": {...}}`. `credentials` is accepted
    and mostly ignored everywhere (registration-api.md section 4), so it's
    typed as a free-form object here rather than repeating its ~15 legacy
    fields on every endpoint; `request_data` is the endpoint's own serializer,
    already written for real validation -- reused here, not duplicated.
    """
    return type(name, (serializers.Serializer,), {
        "__doc__": f"Envelope wrapper for {request_data_serializer_class.__name__}.",
        "credentials": serializers.DictField(
            required=False,
            help_text="Standard credentials block. Accepted on every endpoint; "
                      "read from only where a field's own docs say so, "
                      "ignored otherwise -- never validated.",
        ),
        "request_data": request_data_serializer_class(required=request_data_required),
    })


# Reachable from any throttled endpoint (a view's own `throttle_classes`) or
# any view at all (an unhandled exception) -- core/api.py's exception_handler
# produces both from the same two fixed messages everywhere, so every view
# that can hit them splats the matching one into its own envelope_responses()
# call rather than retyping the message.
RATE_LIMITED = (429, "rate_limited", "Too many requests. Try again later.", {})
SERVER_ERROR = (500, "server_error", "Something went wrong. Try again.", {})


class AppJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    """Registers apps.portal.authentication.AppJWTAuthentication as a bearer
    scheme, so Swagger UI's "Authorize" button offers it -- without this,
    drf-spectacular has no way to know a custom BaseAuthentication subclass
    means `Authorization: Bearer <token>`."""

    target_class = "apps.portal.authentication.AppJWTAuthentication"
    name = "AppBearerAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
