"""The Roles, Users and Privileges screens.

Kept out of apps/portal/views.py, which owns sign-in and the dashboard: those
are the shell, these are ordinary screens and read the way every other module's
do.

The privilege screen is included by each module with its own module code, so
there is one template and one write path, not sixteen
(design/rbac.md section 6).
"""

import json

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from apps.company import writes as company_writes
from apps.company.scoping import companies_for
from apps.crew.scoping import employees_for
from apps.portal import drawers, forms, privileges, scoping, services
from apps.portal.auth import close_sessions_for_user
from apps.portal.models import Module, Page, Role, RolePermission
from apps.portal.permissions import PagePermissionMixin
from apps.portal.services import has_permission
from apps.portal.session_models import LogoutReason
from core.enums import Channel
from core.models import User
from theme import drawers as theme_drawers
from theme.views import ThemedTemplateView

# The flags a screen's buttons check. Defined here rather than imported from
# apps.company.views: the module screens import this file, so reaching back
# into one of them would close a circle.
ACTIONS = ("create", "read", "update", "delete", "approve", "print")


class PortalScreenView(PagePermissionMixin, ThemedTemplateView):
    """Shared by the portal screens: the lists their drawers resolve against,
    and the permission flags their buttons check."""

    drawer_specs = drawers.SPECS

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update({
            "companies_list": list(companies_for(user).filter(is_active=True).values("id", "name")),
            "roles_list": list(scoping.roles_for(user).filter(is_active=True).values("id", "name")),
            "employees_list": [
                {"id": e.pk, "name": f"{e.employee_code} — {e.full_name}"}
                for e in employees_for(user).filter(is_active=True)
            ],
            "channels_list": [{"id": value, "name": label} for value, label in Channel.choices],
            "perm": {
                action: has_permission(user, self.page_code, action) for action in ACTIONS
            },
        })
        return context

    def render_to_response(self, context, **response_kwargs):
        specs = theme_drawers.all_for_user(
            self.drawer_specs, self.request.user.sees_every_company
        )
        context.update(theme_drawers.resolve_all(specs, context))
        return super().render_to_response(context, **response_kwargs)


# --- Roles -------------------------------------------------------------------

class RoleListView(PortalScreenView):
    template_name = "portal/role_list.html"
    page_code = "system.role"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        roles = scoping.roles_for(self.request.user).select_related("company")

        rows = []
        for i, role in enumerate(roles):
            permissions = list(role.permissions.select_related("page__module"))
            modules = {row.page.module.name for row in permissions}
            rows.append({
                "pk": role.pk,
                "name": role.name,
                "description": role.description,
                "company": role.company.name if role.company_id else "Every company",
                "is_system": role.company_id is None,
                "users": role.users.count(),
                "modules": len(modules),
                "module_names": ", ".join(sorted(modules)),
                "active": role.is_active,
                "json_id": f"scr-record-role-{i}",
                "fields_json": {
                    "pk": role.pk,
                    "name": role.name,
                    "description": role.description,
                    "company": role.company_id or "",
                    "is_active": role.is_active,
                },
            })

        context.update({
            "roles": rows,
            "active_count": sum(1 for row in rows if row["active"]),
            "user_count": scoping.users_for(self.request.user).filter(is_active=True).count(),
            "save_url_role": reverse("portal-role-save"),
            "delete_url_role": reverse("portal-role-delete", args=[0]),
            "noun_role": "Role",
        })
        return context


class RoleSave(company_writes.EntitySaveView):
    model = Role
    form_class = forms.RoleForm
    page_code = "system.role"
    noun = "Role"
    scoped = False

    def rows(self):
        return scoping.roles_for(self.request.user)


class RoleDelete(company_writes.EntityDeleteView):
    model = Role
    page_code = "system.role"
    noun = "Role"
    scoped = False

    def rows(self):
        return scoping.roles_for(self.request.user)


# --- Users -------------------------------------------------------------------

class UserListView(PortalScreenView):
    template_name = "portal/user_list.html"
    page_code = "system.user"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        users = scoping.users_for(self.request.user).select_related("role", "employee", "company")

        rows = []
        for i, account in enumerate(users):
            rows.append({
                "pk": account.pk,
                "username": account.username,
                "display_name": account.display_name,
                "role": account.role.name if account.role_id else "",
                "employee": account.employee.full_name if account.employee_id else "",
                "company": account.company.name if account.company_id else "Every company",
                "channels": ", ".join(
                    Channel(value).label for value in (account.allowed_channels or [])
                ),
                "scope": account.get_scope_display(),
                "locked": bool(account.locked_until),
                "active": account.is_active,
                "json_id": f"scr-record-user-{i}",
                "fields_json": {
                    "pk": account.pk,
                    "username": account.username,
                    "display_name": account.display_name,
                    "email": account.email,
                    "mobile": account.mobile,
                    "role": account.role_id or "",
                    "employee": account.employee_id or "",
                    "allowed_channels": list(account.allowed_channels or []),
                    "company": account.company_id or "",
                    "is_active": account.is_active,
                    "password": "",
                },
            })

        context.update({
            "users": rows,
            "active_count": sum(1 for row in rows if row["active"]),
            "locked_count": sum(1 for row in rows if row["locked"]),
            "roleless_count": sum(1 for row in rows if not row["role"]),
            "save_url_user": reverse("portal-user-save"),
            "delete_url_user": reverse("portal-user-delete", args=[0]),
            "reset_url_user": reverse("portal-user-reset-password", args=[0]),
            "unlock_url_user": reverse("portal-user-unlock", args=[0]),
            "noun_user": "User",
        })
        return context


class UserSave(company_writes.EntitySaveView):
    model = User
    form_class = forms.UserForm
    page_code = "system.user"
    noun = "User"
    scoped = False

    def rows(self):
        return scoping.users_for(self.request.user)


class UserDelete(company_writes.EntityDeleteView):
    model = User
    page_code = "system.user"
    noun = "User"
    scoped = False

    def rows(self):
        return scoping.users_for(self.request.user)


class UserResetPassword(company_writes.WriteView):
    """Set someone else's password.

    The new one is typed here and never shown back. Every session the account
    holds is closed: a password is changed because the old one is no longer
    trusted, so anything still signed in with it must stop.
    """

    model = User
    page_code = "system.user"

    def rows(self):
        return scoping.users_for(self.request.user)

    def post(self, request, pk, *args, **kwargs):
        if not self.may("update"):
            return self.refused()

        account = get_object_or_404(self.rows(), pk=pk)
        password = _payload(request).get("password") or ""
        if len(password) < 8:
            return JsonResponse({
                "ok": False, "code": "invalid",
                "errors": [{"field": "Password", "message": "Use at least 8 characters."}],
            }, status=400)

        with transaction.atomic():
            account.set_password(password)
            account.failed_login_count = 0
            account.locked_until = None
            account.save(update_fields=["password", "failed_login_count", "locked_until"])
            close_sessions_for_user(account, LogoutReason.FORCED)

        return JsonResponse({"ok": True, "message": f"Password reset for {account.username}."})


class UserUnlock(company_writes.WriteView):
    """Clear a lockout before its five minutes are up."""

    model = User
    page_code = "system.user"

    def rows(self):
        return scoping.users_for(self.request.user)

    def post(self, request, pk, *args, **kwargs):
        if not self.may("update"):
            return self.refused()

        account = get_object_or_404(self.rows(), pk=pk)
        account.failed_login_count = 0
        account.locked_until = None
        account.save(update_fields=["failed_login_count", "locked_until"])
        return JsonResponse({"ok": True, "message": f"{account.username} can sign in again."})


# --- Privileges --------------------------------------------------------------

class PrivilegeScreenView(PagePermissionMixin, ThemedTemplateView):
    """One module's role x screen x permission grid.

    Each module subclasses this with its own `module_code` and `page_code`; the
    template and every write below are shared.
    """

    template_name = "portal/privileges.html"
    module_code = None
    module_label = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(privileges.matrix_context(self.request.user, self.module_code))
        context.update({
            "title": "Privilege Management",
            "parent": self.module_label,
            "subtitle": (
                f"Which roles may open the {self.module_label} screens, and what they "
                "may do on each. A role added here starts with nothing ticked."
            ),
            "module": self.module_label,
            "can_update": has_permission(self.request.user, self.page_code, "update"),
            "priv_add_url": reverse("portal-privilege-add"),
            "priv_remove_url": reverse("portal-privilege-remove"),
            "priv_save_url": reverse("portal-privilege-save"),
        })
        return context


def _payload(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return {}
    return request.POST.dict()


class PrivilegeWriteView(View):
    """Shared checks: the permission, the module, and the role's scope."""

    def resolve(self, request):
        """(module, role, error_response). Either the first two, or the third."""
        data = _payload(request)
        module = Module.objects.filter(code=data.get("module"), is_active=True).first()
        if module is None:
            return None, None, None, _error("That module does not exist.", status=404)

        page_code = f"{module.code}.privileges"
        if not has_permission(request.user, page_code, "update"):
            return None, None, None, _error(
                "You do not have permission to change privileges here.", status=403
            )

        role = scoping.roles_for(request.user).filter(pk=data.get("role")).first()
        if role is None:
            return None, None, None, _error("That role does not exist.", status=404)

        return module, role, data, None

    def locks_me_out(self, request, module, role, keeping):
        """True when this write would take the acting user's own access to the
        screen they are standing on.

        Without it one save can remove permissions from everybody, including the
        person making it, and the only way back is a shell. Nothing else on the
        system can undo it, so it is refused here rather than warned about.
        """
        if getattr(request.user, "role_id", None) != role.pk:
            return False
        if request.user.sees_every_company:
            # Platform staff can always reach it through another company's copy
            # of the screen; this one is not their only door.
            return False
        return not {"read", "update"}.issubset(keeping)


def _error(message, status=400, field=""):
    return JsonResponse(
        {"ok": False, "code": "refused", "errors": [{"field": field, "message": message}]},
        status=status,
    )


class PrivilegeAddRole(PrivilegeWriteView):
    """Give a role this module: every screen listed, nothing ticked."""

    def post(self, request, *args, **kwargs):
        module, role, _data, error = self.resolve(request)
        if error:
            return error

        with transaction.atomic():
            services.add_role_to_module(role, module)
        return JsonResponse({"ok": True, "message": f"{role.name} added to {module.name}."})


class PrivilegeRemoveRole(PrivilegeWriteView):
    """Take this module away from a role, leaving its other modules alone."""

    def post(self, request, *args, **kwargs):
        module, role, _data, error = self.resolve(request)
        if error:
            return error

        if self.locks_me_out(request, module, role, keeping=set()):
            return _error(
                "That would remove your own access to this screen, and nobody could "
                "put it back from here.", status=409,
            )

        with transaction.atomic():
            services.remove_role_from_module(role, module)
        return JsonResponse({"ok": True, "message": f"{role.name} removed from {module.name}."})


class PrivilegeSave(PrivilegeWriteView):
    """Write one role's grid for one module.

    The payload is what is ticked -- `{"page.code": ["read", "update"]}` -- and
    everything else is cleared, so unticking is as real as ticking. An action a
    screen does not offer is dropped rather than refused: the grid never showed
    a box for it.
    """

    def post(self, request, *args, **kwargs):
        module, role, data, error = self.resolve(request)
        if error:
            return error

        grants = data.get("grants") or {}
        if not isinstance(grants, dict):
            return _error("The grid could not be read.")

        pages = list(Page.objects.filter(module=module, is_active=True))
        existing = {
            row.page_id: row
            for row in RolePermission.objects.filter(role=role, page__in=pages)
        }
        if not existing:
            return _error(
                f"{role.name} is not on this module yet. Add it first, then tick what "
                "it may do.", status=409,
            )

        privilege_page = f"{module.code}.privileges"
        keeping = {
            action for action in (grants.get(privilege_page) or [])
        }
        if self.locks_me_out(request, module, role, keeping):
            return _error(
                "That would remove your own access to this screen, and nobody could "
                "put it back from here.", status=409,
            )

        with transaction.atomic():
            for page in pages:
                permission = existing.get(page.pk)
                if permission is None:
                    continue
                ticked = set(grants.get(page.code) or []) & set(page.actions)
                for action in privileges.ACTION_ORDER:
                    setattr(permission, f"can_{action}", action in ticked)
                permission.save()

        return JsonResponse({"ok": True, "message": f"{role.name} privileges saved."})
