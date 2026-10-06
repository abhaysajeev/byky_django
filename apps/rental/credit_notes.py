"""Credit notes: a partial refund on a settled order, against its invoice.

Ported from the legacy credit note (analysis/rental/credit-note.md):
APISaveOrderCreditNoteRequest (tablet request), DMSUpdateOrderCreditNoteStatus
(approve / reject), DMSUpdateCreditNote (direct issue), APIGetOrderCreditNote-
RequestDetails (tablet status). Kept: the tablet asks, the back office decides
the amount, or issues one directly; one per order. Not kept (the defects in that
analysis, section 9): approval and direct issue now treat money the same -- the
order, invoice and payments are never changed; the amount is checked; a
rejection is kept, not deleted; a repeated request is answered, never dropped.

Rules agreed with the owner (5-6 Oct 2026): only a settled order; one pending
blocks a new request, a rejected or cancelled one does not; the only amount check
is 0 < net <= the order's net; VAT is the share already inside net, at the
invoice's rate; number = order number + "CN".
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.rental.models import (
    CREDIT_NOTE_LIVE,
    CreditNote,
    CreditNoteSource,
    CreditNoteStatus,
    Invoice,
    Order,
    OrderAction,
    OrderStatus,
)
from apps.rental.services import OrderRefused, run_once
from core.ids import uuid7

CENT = Decimal("0.01")


class CreditNoteRefused(Exception):
    """A web action that cannot be done -- the message is shown as it is."""


def vat_split(net, rate):
    """(tax, taxable) inside a VAT-inclusive `net` at `rate` percent:
    tax = net x rate / (100 + rate), to the fil; taxable = net - tax, so the
    two always add back to net."""
    net, rate = Decimal(net), Decimal(rate or 0)
    tax = (net * rate / (100 + rate)).quantize(CENT, rounding=ROUND_HALF_UP) if rate else Decimal("0.00")
    return tax, net - tax


def credit_note_no(order):
    return f"{order.order_no}CN"


def _live(order):
    """The order's pending request or issued credit note, if any."""
    return order.credit_notes.filter(status__in=CREDIT_NOTE_LIVE).first()


def _refuse_if_live(order):
    live = _live(order)
    if live is not None:
        raise OrderRefused("credit_note_pending" if live.status == CreditNoteStatus.PENDING else "credit_note_issued")


def note_json(note):
    """A credit note as the tablet sees it."""
    approved = note.status == CreditNoteStatus.APPROVED
    return {
        "credit_note_id": str(note.pk),
        "order_id": str(note.order_id),
        "order_no": note.order.order_no,
        "source": note.source,
        "status": note.status,
        "credit_note_no": note.credit_note_no or None,
        "net_amount": str(note.net_amount) if approved else None,
        "tax_amount": str(note.tax_amount) if approved else None,
        "taxable_amount": str(note.taxable_amount) if approved else None,
        "reason": note.reason,
        "decision_remark": note.decision_remark,
        "decided_at": note.decided_at.isoformat() if note.decided_at else None,
    }


# -- Tablet --------------------------------------------------------------------------


def request_credit_note(session, values, request_data):
    """A tablet asks for a credit note on a settled order. Returns (reply,
    done_now). The call's sync_id becomes the credit note's id. Any tablet of
    the company may ask, for any of its orders."""
    company = session.branch.company

    def apply():
        # Lock the order only: its invoice is an outer join, which Postgres
        # will not lock.
        order = (Order.objects.select_for_update(of=("self",)).select_related("invoice")
                 .filter(pk=values["order_id"], company=company).first())
        if order is None:
            raise OrderRefused("order_not_synced")
        if order.status != OrderStatus.COMPLETED or not hasattr(order, "invoice"):
            raise OrderRefused("order_not_settled")
        _refuse_if_live(order)
        note = CreditNote.objects.create(
            id=values["sync_id"], company=company, branch=order.branch, order=order, invoice=order.invoice,
            source=CreditNoteSource.DEVICE, status=CreditNoteStatus.PENDING,
            reason=(values.get("reason") or "").strip(), device=session.device, requested_by=session.user,
            requested_at=values["requested_at"], created_by=session.user, modified_by=session.user,
        )
        event = {
            "action": OrderAction.CREDIT_NOTE_REQUEST, "happened_at": values["requested_at"],
            "device": session.device, "user": session.user, "detail": {"credit_note_id": str(note.pk)},
        }
        return order, event, note_json(note)

    return run_once(event_id=values["sync_id"], company=company, request_data=request_data, apply=apply)


def cancel_credit_note(session, values, request_data):
    """A tablet withdraws a request still waiting. Approved or rejected ones
    are final (credit_note_closed); cancelling twice answers the same."""
    company = session.branch.company

    def apply():
        note = (CreditNote.objects.select_for_update().select_related("order")
                .filter(pk=values["credit_note_id"], company=company).first())
        if note is None:
            raise OrderRefused("unknown_credit_note")
        if note.status in (CreditNoteStatus.APPROVED, CreditNoteStatus.REJECTED):
            raise OrderRefused("credit_note_closed")
        if note.status == CreditNoteStatus.PENDING:
            note.status, note.decided_at = CreditNoteStatus.CANCELLED, values["cancelled_at"]
            note.modified_by = session.user
            note.save()
        event = {
            "action": OrderAction.CREDIT_NOTE_CANCEL, "happened_at": values["cancelled_at"],
            "device": session.device, "user": session.user, "detail": {"credit_note_id": str(note.pk)},
        }
        return note.order, event, note_json(note)

    return run_once(event_id=values["sync_id"], company=company, request_data=request_data, apply=apply)


def credit_notes_for_orders(company, order_ids):
    """Every credit note on these orders -- requested by a tablet or issued on
    the web -- newest first."""
    return list(
        CreditNote.objects.filter(company=company, order_id__in=order_ids)
        .select_related("order").order_by("-created_on")
    )


# -- Web -------------------------------------------------------------------------------


def _amount(order, net_amount):
    """The one check on the amount: more than 0, not more than the order's net."""
    try:
        net = Decimal(str(net_amount)).quantize(CENT)
    except (ArithmeticError, ValueError):
        raise CreditNoteRefused("Enter the credit note amount.") from None
    if net <= 0:
        raise CreditNoteRefused("The credit note amount must be more than 0.")
    if order.net_amount is not None and net > order.net_amount:
        raise CreditNoteRefused(f"The credit note cannot be more than the order's net amount, AED {order.net_amount}.")
    return net


def _issue(note, net, user, remark, now):
    tax, taxable = vat_split(net, note.invoice.tax_percentage)
    note.status = CreditNoteStatus.APPROVED
    note.credit_note_no = credit_note_no(note.order)
    note.net_amount, note.tax_percentage = net, note.invoice.tax_percentage
    note.tax_amount, note.taxable_amount = tax, taxable
    note.decided_by, note.decided_at, note.decision_remark = user, now, remark
    note.modified_by = user


def approve(user, note_id, company_ids, net_amount, remark=""):
    """Issue a waiting request with the amount the back office typed."""
    with transaction.atomic():
        note = (CreditNote.objects.select_for_update().select_related("order", "invoice")
                .filter(pk=note_id, company_id__in=company_ids).first())
        if note is None:
            raise CreditNoteRefused("No such credit note request.")
        if note.status != CreditNoteStatus.PENDING:
            raise CreditNoteRefused(f"This request is already {note.get_status_display().lower()}.")
        _issue(note, _amount(note.order, net_amount), user, remark.strip(), timezone.now())
        note.save()
    return note


def reject(user, note_id, company_ids, remark=""):
    with transaction.atomic():
        note = CreditNote.objects.select_for_update().filter(pk=note_id, company_id__in=company_ids).first()
        if note is None:
            raise CreditNoteRefused("No such credit note request.")
        if note.status != CreditNoteStatus.PENDING:
            raise CreditNoteRefused(f"This request is already {note.get_status_display().lower()}.")
        note.status, note.decided_by, note.decided_at = CreditNoteStatus.REJECTED, user, timezone.now()
        note.decision_remark, note.modified_by = remark.strip(), user
        note.save()
    return note


def issue_directly(user, invoice_id, company_ids, net_amount, reason=""):
    """The back office issues a credit note on an invoice with no tablet
    request -- approved at once. Not while a request is waiting on that order:
    that request is approved or rejected instead."""
    try:
        with transaction.atomic():
            invoice = (Invoice.objects.select_related("order")
                       .filter(pk=invoice_id, company_id__in=company_ids).first())
            if invoice is None:
                raise CreditNoteRefused("No such invoice.")
            order = Order.objects.select_for_update().get(pk=invoice.order_id)
            live = _live(order)
            if live is not None:
                raise CreditNoteRefused(
                    "A credit note request is waiting for this order -- approve or reject it instead."
                    if live.status == CreditNoteStatus.PENDING else "This order already has a credit note.")
            note = CreditNote(
                id=uuid7(), company=invoice.company, branch=invoice.branch, order=order, invoice=invoice,
                source=CreditNoteSource.WEB, reason=reason.strip(), created_by=user,
            )
            _issue(note, _amount(order, net_amount), user, "", timezone.now())
            note.save(force_insert=True)
    except IntegrityError:
        raise CreditNoteRefused("This order already has a credit note.") from None
    return note
