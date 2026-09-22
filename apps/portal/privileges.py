"""The privilege screen, shared by every module.

One screen, one template, included by each module with its own module code --
the same shape the wireframe had, now reading and writing real rows.

Two things the wireframe could not know, because it had no data behind it:

* **A cell exists only where the screen offers that action.** `Page.actions`
  varies -- Attendance is read and print, because punches arrive from the apps
  and are never typed in -- and `has_permission()` already refuses an action a
  page does not offer. A checkbox that can never mean anything is worse than no
  checkbox: it looks like a grant that was made.
* **Everything is keyed by primary key, not by role name.** Names are editable,
  and two companies may each have an "Operations".
"""

from apps.portal.models import Action, Module, Page, RolePermission
from apps.portal.scoping import roles_for

# Read first: it is the one that decides whether the screen appears at all.
ACTION_ORDER = [
    Action.READ, Action.CREATE, Action.UPDATE, Action.DELETE,
    Action.PRINT, Action.RECOMMEND, Action.APPROVE,
]

# "Access" is the word the FSD and the legacy screens use for being allowed to
# open a screen at all; ours is the same flag.
ACTION_LABELS = {
    Action.READ: "Access",
    Action.CREATE: "Create",
    Action.UPDATE: "Update",
    Action.DELETE: "Delete",
    Action.PRINT: "Print",
    Action.RECOMMEND: "Recommend",
    Action.APPROVE: "Approve",
}


def module_actions(pages):
    """The columns this module's matrix shows: every action at least one of its
    screens offers, in a fixed order so two modules never disagree on where
    Delete sits."""
    offered = {action for page in pages for action in page.actions}
    return [action for action in ACTION_ORDER if action in offered]


def matrix_context(user, module_code):
    """Everything byky/partials/privilege_matrix.html needs."""
    module = Module.objects.filter(code=module_code).first()
    if module is None:
        return {"roles": [], "mapped_roles": [], "permissions": [], "role_matrices": []}

    pages = list(Page.objects.filter(module=module, is_active=True).order_by("sort_order", "name"))
    actions = module_actions(pages)
    roles = list(roles_for(user).filter(is_active=True))

    granted = {
        (row.role_id, row.page_id): row
        for row in RolePermission.objects.filter(
            role__in=roles, page__module=module
        )
    }
    mapped_ids = {role_id for role_id, _ in granted}

    mapped, unmapped, matrices = [], [], []
    for role in roles:
        if role.pk not in mapped_ids:
            unmapped.append({"pk": role.pk, "name": role.name})
            continue

        screens, full = [], True
        for page in pages:
            permission = granted.get((role.pk, page.pk))
            cells = []
            for action in actions:
                offers = action in page.actions
                is_granted = bool(
                    offers and permission and getattr(permission, f"can_{action}")
                )
                if offers and not is_granted:
                    full = False
                cells.append({"action": action, "offered": offers, "granted": is_granted})
            screens.append({"page": page.code, "name": page.name, "cells": cells})

        mapped.append({
            "pk": role.pk,
            "name": role.name,
            "is_system": role.company_id is None,
            "is_full": full,
        })
        matrices.append({"pk": role.pk, "name": role.name, "screens": screens})

    return {
        "priv_module": module.code,
        "priv_module_name": module.name,
        "roles": unmapped,
        "mapped_roles": mapped,
        "screens": [page.name for page in pages],
        "permissions": [ACTION_LABELS[action] for action in actions],
        "role_matrices": matrices,
    }
