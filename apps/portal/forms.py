"""Forms for the Roles and Users screens.

Both sit on the same rule as the company forms: **the company is never read
from the payload.** It comes from the signed-in user's scope, and a system user
picks one from what they may see.

Two rules are specific to this module:

* a user's role must belong to the user's own company, and a system role may
  only be held by a system user -- otherwise one company's role would grant
  rights over another's rows;
* `scope` is not editable here. Cross-company access is granted deliberately,
  through `manage.py set_user_scope`, never by a dropdown on a screen anyone
  with Users/update can reach.
"""

from django import forms

from apps.company.forms import ScopedModelForm
from apps.portal.models import Role
from apps.portal.scoping import roles_for
from core.enums import Channel
from core.models import User


class RoleForm(ScopedModelForm):
    class Meta:
        model = Role
        fields = ["company", "name", "description", "is_active"]
        labels = {"name": "Role Name", "description": "Description"}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, user=user, **kwargs)
        # A system user may leave the company empty, which makes it a system
        # role -- BYKY's own, held by platform staff across every company.
        if self.user is not None and self.user.sees_every_company:
            self.fields["company"].required = False
            self.fields["company"].help_text = "Leave empty for a BYKY system role."


class UserForm(ScopedModelForm):
    """The account, not the person: the person is the employee record."""

    password = forms.CharField(required=False, label="Password")
    allowed_channels = forms.MultipleChoiceField(
        required=False, choices=Channel.choices, label="Allowed apps"
    )

    class Meta:
        model = User
        fields = [
            "company", "username", "display_name", "email", "mobile",
            "role", "employee", "is_active",
        ]
        labels = {
            "username": "Username", "display_name": "Display Name",
            "email": "Email Address", "mobile": "Mobile Number",
            "role": "Role", "employee": "Employee",
        }

    def __init__(self, *args, user=None, **kwargs):
        # Named, not swallowed by **kwargs: apps/company/writes.py inspects this
        # signature to decide whether to pass the signed-in user, and a form
        # that hides it silently loses its scoping.
        super().__init__(*args, user=user, **kwargs)
        self.fields["role"].queryset = roles_for(self.user).filter(is_active=True)
        self.fields["employee"].required = False
        # scope is not on this form, so every account made here is a company
        # account -- and the database will not accept one without a company.
        self.fields["company"].required = True

        if self.user is not None:
            from apps.crew.scoping import employees_for

            self.fields["employee"].queryset = employees_for(self.user).filter(is_active=True)

        # A password is set once, on create, and afterwards only by Reset
        # password. It is never shown, and an empty box on edit means "leave it".
        if self.instance.pk is None:
            self.fields["password"].required = True

    def clean_username(self):
        # Stored lower-case, so one person is one login however they type it
        # (design/03-login.md section 3).
        username = User.normalize_username(self.cleaned_data.get("username"))
        if not username:
            raise forms.ValidationError("A username is needed.")
        clash = User.objects.filter(username=username)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean(self):
        cleaned = super().clean()
        company = cleaned.get("company")
        role = cleaned.get("role")
        employee = cleaned.get("employee")

        if role is not None:
            if role.company_id is None:
                # A system role sees every company, so only a system user may
                # hold one.
                if self.instance.pk is None or not self.instance.sees_every_company:
                    self.add_error("role", "A system role can only be given to a BYKY system user.")
            elif company is not None and role.company_id != company.pk:
                self.add_error("role", f"{role.name} belongs to another company.")

        if employee is not None:
            if company is not None and employee.company_id != company.pk:
                self.add_error("employee", f"{employee.full_name} belongs to another company.")
            # An employee's login is their employee code, so the two can never
            # drift apart (design/03-login.md section 3).
            cleaned["username"] = User.normalize_username(employee.employee_code)

        return cleaned

    def save(self, commit=True):
        creating = self.instance.pk is None
        user = super().save(commit=False)
        user.username = self.cleaned_data["username"]
        user.allowed_channels = self.cleaned_data.get("allowed_channels") or []
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
        if creating and not password:
            user.set_unusable_password()
        if commit:
            user.save()
        return user
