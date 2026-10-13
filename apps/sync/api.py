"""POST /api/v1/{app}/sync/issues/status -- what the office has done with
this device's parked calls. Read-only: a tablet asks before logout, or to
clear cards it parked earlier."""

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.portal.authentication import AppJWTAuthentication
from apps.sync.models import IssueStatus, SyncIssue
from core.api import envelope, request_parts
from core.schema import SERVER_ERROR, envelope_request, envelope_responses

MAX_IDS = 100
MAX_ROWS = 500

_DESCRIPTION = """
The state of this **device's** parked calls -- by `issue_ids` (up to 100), or,
with none sent, every one the server still keeps (open ones always; resolved
and dismissed ones for 45 days), newest first, at most 500.

`status` is `open` (still with the office), `resolved` (replayed and recorded --
drop the card) or `dismissed` (the office gave up on it; `dismiss_reason`
says why). An open call waiting behind an earlier one for the same order has
`waiting_on`; one dismissed with that earlier call has `dismissed_with`.
"""

_SAMPLE = {"issues": [
    {"issue_id": 12, "sync_id": "01923f8e-5b2a-7c3d-9e4f-a1b2c3d4e5f6", "kind": "settle",
     "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "status": "open", "waiting_on": None,
     "dismissed_with": None, "dismiss_reason": "",
     "error": {"code": "amount_mismatch", "message": "The amounts do not add up."}, "resolved_at": None},
    {"issue_id": 13, "sync_id": "01923f8e-6c3b-7d4e-8f50-b2c3d4e5f6a7", "kind": "return",
     "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9", "status": "open", "waiting_on": 12,
     "dismissed_with": None, "dismiss_reason": "", "error": None, "resolved_at": None},
]}


class IssueStatusRequest(serializers.Serializer):
    issue_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1, error_messages={"invalid": "must be whole numbers"}),
        required=False, max_length=MAX_IDS,
        error_messages={"max_length": f"send at most {MAX_IDS} issue_ids", "not_a_list": "must be a list"},
    )


def _heads(issues):
    """order_id -> its head (lowest open id), for the orders on this page."""
    keys = {i.order_id for i in issues if i.order_id and i.status == IssueStatus.OPEN}
    heads = {}
    for order_id, pk in (SyncIssue.objects.filter(order_id__in=keys, status=IssueStatus.OPEN)
                         .order_by("id").values_list("order_id", "id")):
        heads.setdefault(order_id, pk)
    return heads


def issue_json(issue, heads):
    head = heads.get(issue.order_id) if issue.status == IssueStatus.OPEN else None
    return {
        "issue_id": issue.pk, "sync_id": str(issue.sync_id), "kind": issue.kind,
        "order_id": str(issue.order_id) if issue.order_id else None,
        "status": issue.status,
        "waiting_on": head if head is not None and head != issue.pk else None,
        "dismissed_with": issue.dismissed_with_id,
        "dismiss_reason": issue.dismiss_reason,
        "error": {"code": issue.error_code, "message": issue.error_message} if issue.error_code else None,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
    }


class IssueStatusView(APIView):
    """POST /api/v1/{app}/sync/issues/status -- any app, this device's issues."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Sync Parking"],
        summary="The state of this device's parked calls",
        description=_DESCRIPTION,
        request=envelope_request("SyncIssueStatusEnvelope", IssueStatusRequest),
        responses=envelope_responses(
            (200, "ok", "Issues.", _SAMPLE),
            (400, "invalid_request", "issue_ids must be a list.", {"errors": {"issue_ids": "must be a list"}}),
            (401, "not_authenticated", "Sign in first.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        _, request_data = request_parts(request)
        form = IssueStatusRequest(data=request_data)
        form.is_valid(raise_exception=True)

        session = request.auth
        issues = SyncIssue.objects.none()
        if session.device_id:
            issues = SyncIssue.objects.filter(device_id=session.device_id).order_by("-id")
        ids = form.validated_data.get("issue_ids")
        issues = list(issues.filter(pk__in=ids) if ids else issues[:MAX_ROWS])
        heads = _heads(issues)
        return envelope("ok", "Issues.", {"issues": [issue_json(i, heads) for i in issues]})
