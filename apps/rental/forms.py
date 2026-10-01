"""Forms for the Rental Management screens."""

from apps.company.forms import ScopedModelForm
from apps.company.models import PaymentMode
from apps.rental.models import Customer, full_number


class CustomerForm(ScopedModelForm):
    class Meta:
        model = Customer
        # customer_code is server-generated (writes.CustomerSave), not a form
        # field -- the wireframe's own Add form has no Customer Code field.
        # id_document has no drawer UI this pass (see models.py's docstring).
        fields = [
            "company", "first_name", "last_name", "gender", "date_of_birth", "nationality",
            "id_type", "id_no",
            "mobile_country_code", "mobile_no", "email", "address", "remarks",
            "is_blocked", "block_reason",
            "is_active",
        ]
        labels = {
            "first_name": "First Name", "last_name": "Last Name", "date_of_birth": "Date of Birth",
            "id_type": "ID Type", "id_no": "Document No",
            "mobile_country_code": "Country Code", "mobile_no": "Phone No",
            "is_blocked": "Blocked", "block_reason": "Block Reason",
        }

    def clean(self):
        """The full number is not a form field (the model computes it), so
        Django's own check of its unique rule never runs -- a duplicate would
        reach the database and fail there. Checked here instead, on Phone No."""
        cleaned = super().clean()
        mobile_full = full_number(cleaned.get("mobile_country_code"), cleaned.get("mobile_no"))
        if mobile_full and "mobile_no" not in self.errors:
            taken = Customer.objects.filter(mobile_full=mobile_full)
            if self.instance.pk:
                taken = taken.exclude(pk=self.instance.pk)
            if taken.exists():
                self.add_error("mobile_no", "A customer with this phone number already exists.")
        return cleaned


class PaymentModeForm(ScopedModelForm):
    class Meta:
        model = PaymentMode
        fields = ["company", "name", "is_active"]
        labels = {"name": "Payment Mode"}
