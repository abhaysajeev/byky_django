"""Saving a customer.

A plain WriteView (not company.writes.EntitySaveView's generic post()):
customer_code is server-generated on create, so the save has to run
CustomerForm.save(commit=False), fill the code in, then save -- one step
more than the generic path handles.
"""

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse

from apps.company.scoping import companies_for
from apps.company.writes import WriteView, _errors, _payload
from apps.rental import credit_notes, services, suspense
from apps.rental import requests as order_requests
from apps.rental.forms import CustomerForm
from apps.rental.models import (
    CreditNote,
    Customer,
    DecisionChannel,
    OrderRequest,
    OrderRequestKind,
    SuspenseEntry,
)
from apps.rental.scoping import customers_for, suspense_for


class CustomerSave(WriteView):
    model = Customer
    page_code = "rental.customer"
    noun = "Customer"

    def rows(self):
        return customers_for(self.request.user)

    def post(self, request, *args, **kwargs):
        data = _payload(request)
        pk = data.get("pk") or None

        if not self.may("update" if pk else "create"):
            return self.refused()

        instance = get_object_or_404(self.rows(), pk=pk) if pk else None
        # Read before validation: a model form writes onto its instance as it cleans.
        was_blocked = bool(instance and instance.is_blocked)
        form = CustomerForm(data, instance=instance, user=request.user)

        if not form.is_valid():
            return JsonResponse({"ok": False, "code": "invalid", "errors": _errors(form)}, status=400)

        with transaction.atomic():
            customer = form.save(commit=False)
            if customer.pk is None:
                customer.customer_code = services.next_customer_code(customer.company)
            services.stamp_block(customer, was_blocked, request.user)
            if was_blocked and not customer.is_blocked:
                customer.block_reason = ""
            customer.save()
            if bool(was_blocked) != bool(customer.is_blocked):
                reason = (form.cleaned_data.get("block_reason") if customer.is_blocked
                          else form.cleaned_data.get("unblock_reason")) or ""
                services.log_block_change(customer, request.user, reason.strip())

        return JsonResponse({"ok": True, "pk": customer.pk, "message": "Customer saved."})


# -- Credit notes ----------------------------------------------------------------------


class _CreditNoteWrite(WriteView):
    model = CreditNote
    page_code = "rental.credit_note"
    action = "approve"

    def company_ids(self):
        return list(companies_for(self.request.user).values_list("id", flat=True))

    def answer(self, act):
        if not self.may(self.action):
            return self.refused()
        try:
            note = act(_payload(self.request))
        except credit_notes.CreditNoteRefused as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": [{"field": "", "message": str(refused)}]},
                                status=400)
        return JsonResponse({"ok": True, "message": self.done(note),
                             "redirect": reverse("rental-credit-note-detail", args=[note.pk])})


class CreditNoteApprove(_CreditNoteWrite):
    """Issue a tablet's request with the amount typed here."""

    def done(self, note):
        return f"Credit note {note.credit_note_no} issued."

    def post(self, request, pk):
        return self.answer(lambda data: credit_notes.approve(
            request.user, pk, self.company_ids(), data.get("net_amount"), str(data.get("remarks") or "")))


class CreditNoteReject(_CreditNoteWrite):
    def done(self, note):
        return "Credit note request rejected."

    def post(self, request, pk):
        return self.answer(lambda data: credit_notes.reject(
            request.user, pk, self.company_ids(), str(data.get("remarks") or "")))


class CreditNoteIssue(_CreditNoteWrite):
    """Issue a credit note on an invoice directly, with no tablet request."""

    action = "create"

    def done(self, note):
        return f"Credit note {note.credit_note_no} issued."

    def post(self, request, invoice_pk):
        return self.answer(lambda data: credit_notes.issue_directly(
            request.user, invoice_pk, self.company_ids(), data.get("net_amount"), str(data.get("reason") or "")))



# -- Requests: decided here, as in the manager app (apps/rental/requests.py) --------


class _RequestWrite(WriteView):
    model = OrderRequest
    page_code = "rental.request"

    def company_ids(self):
        return list(companies_for(self.request.user).values_list("id", flat=True))

    def answer(self, act, message, redirect=None):
        """`message` is the text, or a function of the request that gives it.
        `redirect` is where the page goes next -- the request's own page unless
        given (Approve and Reject go back to the Pending list, owner 19 Nov 2026)."""
        if not self.may("approve"):
            return self.refused()
        try:
            req = act(_payload(self.request))
        except order_requests.RequestRefused as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": [{"field": "", "message": refused.message}]},
                                status=400)
        return JsonResponse({"ok": True, "message": message(req) if callable(message) else message,
                             "redirect": redirect or reverse("rental-request-detail", args=[req.pk])})


def pending_list_url():
    """Where Approve and Reject land: the Requests list on its Pending tab,
    without filters (owner, 19 Nov 2026)."""
    return reverse("rental-request-list") + "?tab=pending"


_APPROVED = {
    OrderRequestKind.DISCOUNT: "Discount approved.",
    OrderRequestKind.COMPLIMENTARY: "Complimentary approved.",
    OrderRequestKind.REPRINT: "Invoice reprint approved.",
    OrderRequestKind.ORDER_REPRINT: "Order receipt reprint approved.",
    OrderRequestKind.CANCEL: "Order cancelled.",
}


class RequestApprove(_RequestWrite):
    """Approve a discount request with a rule: a percentage or an AED amount."""

    def post(self, request, pk):
        return self.answer(lambda data: order_requests.approve(
            request.user, pk, self.company_ids(), DecisionChannel.WEB, data.get("discount_type"),
            data.get("discount_value"), str(data.get("note") or "")), lambda req: _APPROVED[req.kind],
            redirect=pending_list_url())


class OrderCancel(_RequestWrite):
    """Cancel a running order from its page, with a reason -- no tablet request.
    The same staff who decide requests may do it."""

    def post(self, request, pk):
        if not self.may("approve"):
            return self.refused()
        reason = str(_payload(request).get("reason") or "")
        try:
            order_requests.cancel_directly(request.user, pk, self.company_ids(), reason)
        except order_requests.RequestRefused as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": [{"field": "", "message": refused.message}]},
                                status=400)
        return JsonResponse({"ok": True, "message": "Order cancelled."})


class InvoiceCancel(_RequestWrite):
    """Cancel a SETTLED order's invoice, and with it the order -- system
    administrators only (owner, 6 Nov 2026; requests.cancel_settled)."""

    def post(self, request, pk):
        if not getattr(request.user, "sees_every_company", False):
            return self.refused()
        reason = str(_payload(request).get("reason") or "")
        try:
            order_requests.cancel_settled(request.user, pk, reason)
        except order_requests.RequestRefused as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": [{"field": "", "message": refused.message}]},
                                status=400)
        return JsonResponse({"ok": True, "message": "Invoice cancelled. Its money no longer counts anywhere."})


class RequestReject(_RequestWrite):
    def post(self, request, pk):
        return self.answer(lambda data: order_requests.reject(
            request.user, pk, self.company_ids(), DecisionChannel.WEB, str(data.get("note") or "")),
            "Request rejected.", redirect=pending_list_url())


class RequestRevoke(_RequestWrite):
    def post(self, request, pk):
        return self.answer(lambda data: order_requests.revoke(
            request.user, pk, self.company_ids(), DecisionChannel.WEB, str(data.get("note") or "")),
            "Approval revoked.")


# -- Suspense entries (owner, 26 Nov 2026; apps/rental/suspense.py) ----------------------
# Create needs Create, edit Update, Resolve Approve, Cancel Delete -- entries are
# never deleted, so the Delete right is what cancels one.


class _SuspenseWrite(WriteView):
    model = SuspenseEntry
    page_code = "rental.suspense"

    def entry(self, pk):
        return get_object_or_404(suspense_for(self.request.user), pk=pk)

    @staticmethod
    def invalid(exc):
        return JsonResponse({"ok": False, "code": "invalid", "errors": exc.errors}, status=400)


class SuspenseSave(_SuspenseWrite):
    def post(self, request, *args, **kwargs):
        data = _payload(request)
        pk = data.get("pk") or None
        if not self.may("update" if pk else "create"):
            return self.refused()
        entry = self.entry(pk) if pk else None
        try:
            entry = suspense.save(request.user, data, entry)
        except suspense.SuspenseInvalid as exc:
            return self.invalid(exc)
        return JsonResponse({"ok": True, "pk": str(entry.pk),
                             "message": "Suspense entry updated." if pk else "Suspense entry added."})


class SuspenseResolve(_SuspenseWrite):
    def post(self, request, pk, *args, **kwargs):
        if not self.may("approve"):
            return self.refused()
        entry = self.entry(pk)
        try:
            suspense.resolve(request.user, entry, _payload(request).get("remarks"))
        except suspense.SuspenseInvalid as exc:
            return self.invalid(exc)
        return JsonResponse({"ok": True, "message": "Marked resolved.",
                             "redirect": reverse("rental-suspense-detail", args=[entry.pk])})


class SuspenseCancel(_SuspenseWrite):
    def post(self, request, pk, *args, **kwargs):
        if not self.may("delete"):
            return self.refused()
        entry = self.entry(pk)
        try:
            suspense.cancel(request.user, entry, _payload(request).get("reason"))
        except suspense.SuspenseInvalid as exc:
            return self.invalid(exc)
        return JsonResponse({"ok": True, "message": "Suspense entry cancelled.",
                             "redirect": reverse("rental-suspense-detail", args=[entry.pk])})
