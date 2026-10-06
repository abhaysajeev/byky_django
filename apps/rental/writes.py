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
from apps.rental.forms import CustomerForm
from apps.rental.models import CreditNote, Customer
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
