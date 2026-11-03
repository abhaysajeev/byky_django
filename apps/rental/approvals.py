"""What the manager app decides besides order requests: credit-note requests
and card-discount approval claims -- listed, and counted with the requests for
the app's badges.

The decisions themselves are the web's own services, called with the manager
as the decider and `DecisionChannel.MANAGER`: credit_notes.approve / reject and
discount.services.decide_claim. Same locks and checks, so whoever decides first
-- on the web or in the app -- wins (owner, 13 Oct 2026).
"""

import datetime

from django.core.paginator import Paginator
from django.db.models import Count
from django.db.models.functions import Coalesce

from apps.crew import leave
from apps.discount.models import CardDiscountClaim, ClaimStatus
from apps.rental.models import (
    CreditNote,
    CreditNoteStatus,
    OrderRequest,
    OrderRequestKind,
    OrderRequestStatus,
)
from apps.rental.requests import PAGE_SIZE, _local, _money, _name, card_photo_fields
from core.timezones import zone_for


def _between(rows, field, zone, from_date, to_date):
    """Rows whose `field` falls on or after from_date and on or before
    to_date -- company-local days, both included; either may be left out."""
    if from_date:
        rows = rows.filter(**{f"{field}__gte": datetime.datetime.combine(from_date, datetime.time.min, tzinfo=zone)})
    if to_date:
        rows = rows.filter(**{f"{field}__lt": datetime.datetime.combine(
            to_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=zone)})
    return rows


def _where(rows, company_ids, state_id=None, branch_id=None):
    rows = rows.filter(company_id__in=company_ids)
    if state_id:
        rows = rows.filter(branch__location__state_id=state_id)
    if branch_id:
        rows = rows.filter(branch_id=branch_id)
    return rows


def _paged(rows, page, to_json):
    page = Paginator(rows, PAGE_SIZE).get_page(page)
    zones = {}
    out = [to_json(row, zones.setdefault(row.company_id, zone_for(row.company))) for row in page.object_list]
    return out, page


# -- Credit notes ----------------------------------------------------------------------

def _note_rows():
    # A note issued straight from the web has no tablet time: it sorts and
    # filters by when it was made.
    return (CreditNote.objects.select_related("order", "invoice", "branch", "company", "requested_by",
                                              "decided_by")
            .annotate(asked_at=Coalesce("requested_at", "created_on")))


def credit_note_json(note, zone):
    approved = note.status == CreditNoteStatus.APPROVED
    order = note.order
    return {
        "credit_note_id": str(note.pk), "status": note.status, "source": note.source,
        "credit_note_no": note.credit_note_no or None,
        "order": {"order_id": str(order.pk), "order_no": order.order_no,
                  "customer_name": order.customer_name, "customer_mobile": order.customer_mobile,
                  "net_amount": _money(order.net_amount)},
        "invoice": {"invoice_no": note.invoice.invoice_no, "net_amount": _money(note.invoice.net_amount)},
        "branch_id": note.branch_id, "station": note.branch.name,
        "requested_by": _name(note.requested_by), "requested_at": _local(note.requested_at, zone),
        "reason": note.reason,
        "net_amount": _money(note.net_amount) if approved else None,
        "tax_amount": _money(note.tax_amount) if approved else None,
        "taxable_amount": _money(note.taxable_amount) if approved else None,
        "decided_by": _name(note.decided_by), "decided_channel": note.decided_channel or None,
        "decided_at": _local(note.decided_at, zone), "decision_remark": note.decision_remark,
    }


def credit_note_list(company_ids, *, pending_only=False, state_id=None, branch_id=None, from_date=None,
                     to_date=None, zone=None, page=1):
    rows = _where(_note_rows(), company_ids, state_id, branch_id).order_by("-asked_at", "-created_on")
    if zone is not None:
        rows = _between(rows, "asked_at", zone, from_date, to_date)
    if pending_only:
        rows = rows.filter(status=CreditNoteStatus.PENDING)
    return _paged(rows, page, credit_note_json)


def credit_note_row(note_id):
    note = _note_rows().get(pk=note_id)
    return credit_note_json(note, zone_for(note.company))


# -- Card-discount claims --------------------------------------------------------------

def _claim_rows():
    return CardDiscountClaim.objects.select_related(
        "order", "branch", "company", "card_type", "card_grade", "requested_by", "decided_by", "revoked_by")


def claim_json(claim, zone):
    return {
        "claim_id": str(claim.pk), "status": claim.status,
        "order": {"order_id": str(claim.order_id), "order_no": claim.order.order_no},
        "customer_name": claim.customer_name, "customer_mobile": claim.mobile_full,
        "card_type": claim.card_type.name, "card_grade": claim.card_grade.name,
        "card_number": claim.card_number, "card_photo": claim.card_photo or None, **card_photo_fields(claim),
        "discount_percent": str(claim.discount_percent), "fare_basis": claim.fare_basis,
        "bill_amount": _money(claim.bill_amount), "discount_amount": _money(claim.discount_amount),
        "net_amount": _money(claim.net_amount),
        "branch_id": claim.branch_id, "station": claim.branch.name if claim.branch_id else None,
        "requested_by": _name(claim.requested_by), "requested_at": _local(claim.requested_at, zone),
        "decided_by": _name(claim.decided_by), "decided_channel": claim.decided_channel or None,
        "decided_at": _local(claim.decided_at, zone), "remarks": claim.remarks,
        "request_remarks": claim.request_remarks,       # the operator's note when asking
        # An approval taken back (owner, 30 Oct 2026): who, where, when and why.
        "revoked_by": _name(claim.revoked_by), "revoked_channel": claim.revoked_channel or None,
        "revoked_at": _local(claim.revoked_at, zone), "revoke_note": claim.revoke_note,
    }


def claim_list(company_ids, *, pending_only=False, status=None, state_id=None, branch_id=None,
               from_date=None, to_date=None, zone=None, page=1):
    rows = _where(_claim_rows(), company_ids, state_id, branch_id).order_by("-requested_at")
    if zone is not None:
        rows = _between(rows, "requested_at", zone, from_date, to_date)
    if pending_only:
        rows = rows.filter(status=ClaimStatus.PENDING)
    if status:
        rows = rows.filter(status=status)
    return _paged(rows, page, claim_json)


def claim_row(claim_id):
    claim = _claim_rows().get(pk=claim_id)
    return claim_json(claim, zone_for(claim.company))


# -- Badges ----------------------------------------------------------------------------

def pending_counts(company_ids, *, state_id=None, branch_id=None):
    """How many of each kind wait for a decision, for the app's badges."""
    by_kind = dict(
        _where(OrderRequest.objects.filter(status=OrderRequestStatus.PENDING), company_ids, state_id, branch_id)
        .values_list("kind").annotate(n=Count("pk")).values_list("kind", "n")
    )
    counts = {kind: by_kind.get(kind, 0) for kind in OrderRequestKind.values}
    counts["credit_note"] = _where(CreditNote.objects.filter(status=CreditNoteStatus.PENDING),
                                   company_ids, state_id, branch_id).count()
    counts["card_discount"] = _where(CardDiscountClaim.objects.filter(status=ClaimStatus.PENDING),
                                     company_ids, state_id, branch_id).count()
    counts["leave"] = leave.pending_count(company_ids, state_id=state_id, branch_id=branch_id)
    from apps.fleet.models import ServiceRequest, ServiceRequestStatus

    counts["service"] = _where(ServiceRequest.objects.filter(status=ServiceRequestStatus.PENDING),
                               company_ids, state_id, branch_id).count()
    counts["total"] = sum(counts.values())
    return counts
