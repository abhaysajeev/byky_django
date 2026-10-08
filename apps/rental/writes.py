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
from apps.rental import credit_notes, services
from apps.rental import requests as order_requests
from apps.rental.forms import CustomerForm
from apps.rental.models import CreditNote, Customer, DecisionChannel, OrderRequest
from apps.rental.scoping import customers_for


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
        form = CustomerForm(data, instance=instance, user=request.user)

        if not form.is_valid():
            return JsonResponse({"ok": False, "code": "invalid", "errors": _errors(form)}, status=400)

        with transaction.atomic():
            customer = form.save(commit=False)
            if customer.pk is None:
                customer.customer_code = services.next_customer_code(customer.company)
            customer.save()

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

    def answer(self, act, message):
        if not self.may("approve"):
            return self.refused()
        try:
            req = act(_payload(self.request))
        except order_requests.RequestRefused as refused:
            return JsonResponse({"ok": False, "code": "invalid", "errors": [{"field": "", "message": refused.message}]},
                                status=400)
        return JsonResponse({"ok": True, "message": message,
                             "redirect": reverse("rental-request-detail", args=[req.pk])})


class RequestApprove(_RequestWrite):
    """Approve a discount request with a rule: a percentage or an AED amount."""

    def post(self, request, pk):
        return self.answer(lambda data: order_requests.approve(
            request.user, pk, self.company_ids(), DecisionChannel.WEB, data.get("discount_type"),
            data.get("discount_value"), str(data.get("note") or "")), "Discount approved.")


class RequestReject(_RequestWrite):
    def post(self, request, pk):
        return self.answer(lambda data: order_requests.reject(
            request.user, pk, self.company_ids(), DecisionChannel.WEB, str(data.get("note") or "")),
            "Request rejected.")


class RequestRevoke(_RequestWrite):
    def post(self, request, pk):
        return self.answer(lambda data: order_requests.revoke(
            request.user, pk, self.company_ids(), DecisionChannel.WEB, str(data.get("note") or "")),
            "Approval revoked.")
