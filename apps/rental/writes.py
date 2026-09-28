"""Saving a customer.

A plain WriteView (not company.writes.EntitySaveView's generic post()):
customer_code is server-generated on create, so the save has to run
CustomerForm.save(commit=False), fill the code in, then save -- one step
more than the generic path handles.
"""

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from apps.company.writes import WriteView, _errors, _payload
from apps.rental import services
from apps.rental.forms import CustomerForm
from apps.rental.models import Customer
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
