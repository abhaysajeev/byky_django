"""Register the crew screens.

Module and Page are developer data -- what the software has. Granting stays a
separate, deliberate step (`manage.py sync_role_pages`), so a new company never
inherits access silently.
"""

from django.db import migrations

CREW_SVG = "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8M4 21a8 8 0 0 1 16 0"

CRUD = ["create", "read", "update", "delete"]
CRUD_PRINT = CRUD + ["print"]

PAGES = [
    ("crew.employee", "Employee", "crew-employee-list", CRUD_PRINT, 1),
    ("crew.designation", "Designation", "crew-designation-list", CRUD_PRINT, 2),
    ("crew.employee_address", "Employee Address", "crew-address-list", CRUD, 3),
    ("crew.block_unblock", "Block / Unblock", "crew-block-unblock", ["read", "update"], 4),
    # Read-only: punches are written by the apps, never from the web.
    ("crew.attendance", "Attendance", "crew-attendance-list", ["read", "print"], 5),
]


def seed(apps, schema_editor):
    Module = apps.get_model("portal", "Module")
    Page = apps.get_model("portal", "Page")

    module, _ = Module.objects.update_or_create(
        code="crew",
        defaults={
            "name": "Crew",
            "sort_order": 20,
            "menu_header": "Operations",
            "is_flat": False,
            "svg": CREW_SVG,
            "svg2": "",
        },
    )

    for code, name, url_name, actions, sort_order in PAGES:
        Page.objects.update_or_create(
            code=code,
            defaults={
                "module": module,
                "name": name,
                "url_name": url_name,
                "actions": actions,
                "channels": ["web"],
                "sort_order": sort_order,
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    apps.get_model("portal", "Page").objects.filter(code__in=[p[0] for p in PAGES]).delete()
    apps.get_model("portal", "Module").objects.filter(code="crew").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0009_session_bigint_key"),
        ("crew", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
