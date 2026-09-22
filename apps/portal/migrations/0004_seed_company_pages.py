"""Register the screens this release ships.

Module and Page are developer data: they describe what the software has, not
what a customer may do with it. Granting is deliberately NOT done here --
`manage.py sync_role_pages` is how a role gets access, so a new company never
silently inherits it (design/rbac.md section 7).

The icon path data is the wireframe's own, lifted from vertical_menu.json so the
sidebar draws the same glyphs.
"""

from django.db import migrations

DASHBOARD_SVG = "M3 10.5 12 3l9 7.5"
DASHBOARD_SVG2 = "M5 9.8V20a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9.8"
COMPANY_SVG = (
    "M4 21V4a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v17M15 9h4a1 1 0 0 1 1 1v11"
    "M3 21h18M8 7h3M8 11h3M8 15h3"
)

CRUD = ["create", "read", "update", "delete"]
CRUD_PRINT = CRUD + ["print"]

MODULES = [
    {
        "code": "general",
        "name": "General",
        "sort_order": 0,
        "menu_header": "",
        "is_flat": True,
        "svg": "",
        "svg2": "",
    },
    {
        "code": "company",
        "name": "Company",
        "sort_order": 10,
        "menu_header": "Operations",
        "is_flat": False,
        "svg": COMPANY_SVG,
        "svg2": "",
    },
]

PAGES = [
    ("general", "general.dashboard", "Dashboard", "dashboard", ["read"], 0,
     DASHBOARD_SVG, DASHBOARD_SVG2),
    ("company", "company.country_state", "Country & State", "company-country-state-list",
     CRUD_PRINT, 1, "", ""),
    ("company", "company.company", "Company", "company-company-list",
     CRUD_PRINT + ["approve"], 2, "", ""),
    ("company", "company.location", "Location", "company-location-list",
     CRUD_PRINT, 3, "", ""),
    ("company", "company.department", "Department", "company-department-list",
     CRUD_PRINT, 4, "", ""),
    ("company", "company.branch", "Branch", "company-branch-list",
     CRUD_PRINT + ["approve"], 5, "", ""),
    ("company", "company.branch_working_time", "Branch Working Time",
     "company-branch-working-time-edit", ["create", "read", "update"], 6, "", ""),
    ("company", "company.branch_department", "Branch Department Mapping",
     "company-branch-department-list", CRUD, 7, "", ""),
    ("company", "company.branch_address", "Station Address Mapping",
     "company-branch-address-list", CRUD + ["approve"], 8, "", ""),
]


def seed(apps, schema_editor):
    Module = apps.get_model("portal", "Module")
    Page = apps.get_model("portal", "Page")

    modules = {}
    for row in MODULES:
        module, _ = Module.objects.update_or_create(
            code=row["code"],
            defaults={k: v for k, v in row.items() if k != "code"},
        )
        modules[row["code"]] = module

    for module_code, code, name, url_name, actions, sort_order, svg, svg2 in PAGES:
        Page.objects.update_or_create(
            code=code,
            defaults={
                "module": modules[module_code],
                "name": name,
                "url_name": url_name,
                "actions": actions,
                "channels": ["web"],
                "sort_order": sort_order,
                "svg": svg,
                "svg2": svg2,
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    Page = apps.get_model("portal", "Page")
    Module = apps.get_model("portal", "Module")
    Page.objects.filter(code__in=[row[1] for row in PAGES]).delete()
    Module.objects.filter(code__in=[row["code"] for row in MODULES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0003_module_is_flat_module_menu_header_module_svg_and_more"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
