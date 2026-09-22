"""Crew forms.

The employee form spans four tables, so it is assembled from three pieces:
`EmployeeForm` for the row itself, `EmployeeDetailForm` for the documents, and
`EmployeeAddressForm` for an address. The view saves them together
(apps/crew/writes.py); the person filling them in sees one drawer.
"""

from django import forms

from apps.company.forms import ScopedModelForm
from apps.crew.models import (
    Designation,
    Employee,
    EmployeeAddress,
    EmployeeDetail,
)


class DesignationForm(ScopedModelForm):
    class Meta:
        model = Designation
        fields = ["company", "code", "title", "description", "rank_order",
                  "attendance_method", "roster_category", "is_active"]
        labels = {
            "code": "Designation Code", "title": "Designation Title",
            "description": "Description", "rank_order": "Rank Order",
            "attendance_method": "Attendance Method",
            "roster_category": "Duty Roster Category",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rank_order"].required = False

    def clean_rank_order(self):
        # No seniority ranking has been supplied, so an empty box means 1
        # rather than an error.
        return self.cleaned_data.get("rank_order") or 1


class EmployeeForm(ScopedModelForm):
    class Meta:
        model = Employee
        fields = [
            "company", "employee_code", "first_name", "middle_name", "last_name",
            "designation", "branch", "attendance_method",
            "date_of_birth", "gender", "marital_status", "nationality",
            "mobile", "email", "emergency_contact_person", "emergency_phone",
            "is_active",
        ]
        labels = {
            "employee_code": "Employee Code",
            "first_name": "First Name", "middle_name": "Middle Name", "last_name": "Last Name",
            "designation": "Designation", "branch": "Branch",
            "attendance_method": "Attendance Method",
            "date_of_birth": "Date of Birth", "gender": "Gender",
            "marital_status": "Marital Status", "nationality": "Nationality",
            "mobile": "Mobile Number", "email": "Email Address",
            "emergency_contact_person": "Emergency Contact Person",
            "emergency_phone": "Emergency Phone",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.user is not None:
            from apps.company.scoping import branches_for
            from apps.crew.scoping import designations_for

            # Only this company's designations and branches.
            self.fields["designation"].queryset = designations_for(self.user).filter(is_active=True)
            self.fields["branch"].queryset = branches_for(self.user).filter(is_active=True)
        self.fields["attendance_method"].required = False


class EmployeeDetailForm(forms.ModelForm):
    """The Documents section of the employee drawer."""

    class Meta:
        model = EmployeeDetail
        fields = [
            "passport_number", "passport_expiry",
            "visa_number", "visa_expiry",
            "emirates_id_number", "emirates_id_expiry",
            "labour_card_number", "labour_card_expiry",
            "salary_bank", "salary_account",
        ]
        labels = {
            "passport_number": "Passport Number", "passport_expiry": "Passport Expiry",
            "visa_number": "Visa Number", "visa_expiry": "Visa Expiry",
            "emirates_id_number": "Emirates ID Number", "emirates_id_expiry": "Emirates ID Expiry",
            "labour_card_number": "Labour Card Number", "labour_card_expiry": "Labour Card Expiry",
            "salary_bank": "Salary Bank", "salary_account": "Salary Account Details",
        }


class EmployeeAddressForm(forms.ModelForm):
    class Meta:
        model = EmployeeAddress
        fields = [
            "employee", "address_type", "line1", "line2", "building", "flat",
            "city", "state", "country", "zip_code",
            "landlord_name", "landlord_phone", "contact_person", "contact_phone",
            "is_current",
        ]
        labels = {
            "employee": "Employee", "address_type": "Address Type",
            "line1": "Address Line 1", "line2": "Address Line 2",
            "building": "Building Name", "flat": "Flat / Room No",
            "city": "City", "state": "State", "country": "Country",
            "zip_code": "Zip Code",
            "landlord_name": "Landlord Name", "landlord_phone": "Landlord Contact Phone",
            "contact_person": "Contact Person Name", "contact_phone": "Contact No",
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user is not None:
            from apps.crew.scoping import employees_for

            # An address can only be attached to an employee this user can see.
            # The state and country are left unscoped on purpose: a home address
            # sits wherever the person lives, not where their employer has
            # branches.
            self.fields["employee"].queryset = employees_for(user)

    def clean(self):
        cleaned = super().clean()
        state, country = cleaned.get("state"), cleaned.get("country")
        if state and country and state.country_id != country.pk:
            self.add_error("state", f"{state.name} is not in {country.name}.")
        return cleaned
