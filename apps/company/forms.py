"""Forms for the Company screens.

One ModelForm per entity. Two rules they all share:

* **The company is never read from the payload.** It comes from the signed-in
  user's scope. A system user belongs to no company, so they choose one -- and
  that choice is checked against what they may see.
* **Labels match the drawer.** An error names "Branch Code", not `short_code`;
  the person reading it has never seen a column name.
"""

from django import forms

from apps.company.models import (
    Branch,
    BranchWorkingTime,
    Company,
    Country,
    Department,
    Location,
    State,
)
from apps.company.scoping import companies_for


class ScopedModelForm(forms.ModelForm):
    """Base for every form on a company-owned table."""

    is_active = forms.BooleanField(required=False, initial=True, label="Active")

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if "company" in self.fields:
            self.fields["company"].label = "Company"
            self.fields["company"].queryset = companies_for(user)
            if user is not None and not user.sees_every_company:
                # Their company is theirs. The field stays in the form -- "unique
                # per company" can only be checked while the company is part of
                # what is being validated, and dropping it would trade a sentence
                # in the drawer for an IntegrityError -- but it is hidden, and any
                # posted value is replaced before validation runs, so a crafted
                # request writes into their own company or nowhere.
                self.fields["company"].widget = forms.HiddenInput()
                if self.is_bound:
                    self.data = {**self.data.dict()} if hasattr(self.data, "dict") else {**self.data}
                    self.data["company"] = user.company_id

    def save(self, commit=True):
        instance = super().save(commit=False)
        if hasattr(instance, "company_id") and not instance.company_id:
            instance.company = self.user.company
        if instance.pk is None and hasattr(instance, "apply_approval_defaults"):
            # Everything saves approved and active for now; the approval flow
            # is wired per screen later (design/02-company.md 3.0). A User
            # carries the same three columns but not the mixin -- an account is
            # never held back for approval -- so it is skipped here.
            instance.apply_approval_defaults()
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class CountryForm(forms.ModelForm):
    class Meta:
        model = Country
        fields = ["short_code", "name", "is_active"]
        labels = {"short_code": "Country Code", "name": "Country Name"}

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.pk is None:
            instance.apply_approval_defaults()
        if commit:
            instance.save()
        return instance


class StateForm(forms.ModelForm):
    class Meta:
        model = State
        fields = ["country", "short_code", "name", "is_active"]
        labels = {"country": "Country", "short_code": "State Code", "name": "State Name"}

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.pk is None:
            instance.apply_approval_defaults()
        if commit:
            instance.save()
        return instance


class LocationForm(forms.ModelForm):
    class Meta:
        model = Location
        fields = ["country", "state", "short_code", "name", "landmark", "is_active"]
        labels = {
            "country": "Country", "state": "State",
            "short_code": "Location Code", "name": "Location Name",
            "landmark": "Landmark",
        }

    def clean(self):
        cleaned = super().clean()
        state, country = cleaned.get("state"), cleaned.get("country")
        if state and country and state.country_id != country.pk:
            self.add_error("state", f"{state.name} is not in {country.name}.")
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.pk is None:
            instance.apply_approval_defaults()
        if commit:
            instance.save()
        return instance


class DepartmentForm(ScopedModelForm):
    class Meta:
        model = Department
        fields = ["company", "short_code", "name", "is_active"]
        labels = {"short_code": "Department Code", "name": "Department Name"}


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = [
            "short_code", "name", "ceo_name", "dto_name", "date_of_commissioning",
            "address", "country", "state", "city", "zip_code",
            "incorporation_certificate_number", "business_certificate_number",
            "income_tax_number", "tax_percentage", "tin", "cst", "service_tax_number",
            "contact_person", "phone_number", "fax_number", "email", "website",
            "is_active",
        ]
        labels = {
            "short_code": "Company Code", "name": "Company Name",
            "ceo_name": "CEO Name", "dto_name": "DTO Name",
            "date_of_commissioning": "Date of Commissioning",
            "zip_code": "ZIP Code",
            "incorporation_certificate_number": "Incorporation Certificate No",
            "business_certificate_number": "Business Certificate No",
            "income_tax_number": "Income Tax No", "tax_percentage": "Tax %",
            "tin": "TIN", "cst": "CST", "service_tax_number": "Service Tax No",
            "phone_number": "Phone Number", "fax_number": "Fax",
            "email": "Email Address", "website": "Web Address",
        }

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.pk is None:
            instance.apply_approval_defaults()
        if commit:
            instance.save()
        return instance


class BranchForm(ScopedModelForm):
    class Meta:
        model = Branch
        fields = [
            "company", "short_code", "name", "location", "branch_type",
            "is_hotel", "accepts_app_payment", "is_multi_device", "allows_test_ride",
            "hotel_commission", "departments",
            "station_number", "address", "latitude", "longitude", "contact_no",
            "is_active",
        ]
        labels = {
            "short_code": "Branch Code", "name": "Branch Name",
            "location": "Location", "branch_type": "Branch Type",
            "is_hotel": "Is Hotel", "accepts_app_payment": "Is App Payment",
            "is_multi_device": "Allow Multiple Devices",
            "allows_test_ride": "Is Test Vehicle",
            "hotel_commission": "Hotel Commission %",
            "departments": "Departments", "station_number": "Station No",
            "address": "Address", "latitude": "Latitude", "longitude": "Longitude",
            "contact_no": "Contact No",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.user is not None:
            # A branch carries only its own company's departments. Locations are
            # deliberately not scoped: they are shared geography, and limiting
            # them to zones that already have a branch would make the first
            # branch in a new zone impossible to create.
            from apps.company.scoping import departments_for

            self.fields["departments"].queryset = departments_for(self.user)


class BranchWorkingTimeForm(forms.ModelForm):
    class Meta:
        model = BranchWorkingTime
        fields = ["branch", "week_day", "shift_number", "start_time", "end_time"]
        labels = {
            "branch": "Branch", "week_day": "Day", "shift_number": "Shift",
            "start_time": "Start Time", "end_time": "End Time",
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_time"), cleaned.get("end_time")
        if start and end and start == end:
            self.add_error("end_time", "Start and end time cannot be the same.")
        return cleaned
