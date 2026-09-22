"""Drawer specs for the Roles and Users screens.

The wireframe had neither screen -- its privilege matrix worked against a
hardcoded list of eight role names -- so these are composed from the same field
kinds every other drawer uses (theme/drawers.py), not ported.

Two fields are deliberately absent:

* **Scope.** Whether an account sees one company or all of them is granted
  through `manage.py set_user_scope`, not from a screen.
* **Blocked.** That belongs to the person, on the crew Block / Unblock screen.
  An account and an employment are different things and are stopped for
  different reasons.
"""

ROLE = {
    "drawer_id": "drawerRole",
    "scr_name": "role",
    "model": "portal.Role",
    "add_label": "Add Role",
    "title_field": "name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "company", "label": "Company", "kind": "select",
                    "required": False, "options_from": "companies_list",
                    "option_key": "name", "system_only": True,
                    "help": "Leave empty for a BYKY system role, which spans every company.",
                },
                {"id": "name", "label": "Role Name", "kind": "text", "required": True},
                {
                    "id": "description", "label": "Description", "kind": "textarea",
                    "required": False, "width": 12,
                    "help": "What this role is for. A new role starts with no access at "
                            "all -- give it screens on each module's Privileges screen.",
                },
            ],
        },
    ],
}

USER = {
    "drawer_id": "drawerUser",
    "scr_name": "user",
    "model": "core.User",
    "add_label": "Add User",
    "title_field": "display_name",
    "sections": [
        {
            "title": "Account",
            "fields": [
                {
                    "id": "company", "label": "Company", "kind": "select",
                    "required": True, "options_from": "companies_list",
                    "option_key": "name", "system_only": True,
                },
                {
                    "id": "employee", "label": "Employee", "kind": "select",
                    "required": False, "options_from": "employees_list", "option_key": "name",
                    "help": "Link a staff record and the username becomes their employee code.",
                },
                {
                    "id": "username", "label": "Username", "kind": "text",
                    "required": True, "lock_on_edit": True,
                    "help": "Stored lower-case. Cannot be changed once the account exists.",
                },
                {"id": "display_name", "label": "Display Name", "kind": "text", "required": True},
                {
                    "id": "password", "label": "Password", "kind": "password",
                    "required": False,
                    "help": "Set once, here. Afterwards use Reset password on the row.",
                },
            ],
        },
        {
            "title": "Access",
            "fields": [
                {
                    "id": "role", "label": "Role", "kind": "select",
                    "required": False, "options_from": "roles_list", "option_key": "name",
                    "help": "Everything this account may do. Without one it can sign in "
                            "and see nothing.",
                },
                {
                    "id": "allowed_channels", "label": "Allowed apps", "kind": "multiselect",
                    "required": False, "options_from": "channels_list", "option_key": "name",
                    "width": 12, "placeholder": "Which apps this account may sign in to",
                },
            ],
        },
        {
            "title": "Contact",
            "fields": [
                {"id": "email", "label": "Email Address", "kind": "text", "required": False},
                {"id": "mobile", "label": "Mobile Number", "kind": "text", "required": False},
            ],
        },
    ],
}

SPECS = {
    "drawer_role": ROLE,
    "drawer_user": USER,
}
