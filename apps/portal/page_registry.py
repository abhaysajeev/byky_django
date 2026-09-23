"""Every screen the software has.

`Module` and `Page` are developer data: they describe what exists, not who may
use it. Granting stays a separate, deliberate act (`sync_role_pages`), so a new
company never inherits access because a screen shipped.

One file rather than a data migration per module, so the whole list is readable
at once. `manage.py sync_pages` writes it, and is safe to re-run.

**A retired screen stays listed, marked retired.** Deleting the row would take
its RolePermission rows with it -- and those are the record of who could once
open it. It also keeps the fact visible here rather than buried in a migration.

Adding a screen: add a line, run `sync_pages`. The sidebar picks it up from the
role's permissions; nothing else to wire.
"""

CRUD = ["create", "read", "update", "delete"]
CRUD_PRINT = CRUD + ["print"]
CRUD_PRINT_APPROVE = CRUD_PRINT + ["approve"]

DASHBOARD_SVG = "M3 10.5 12 3l9 7.5"
DASHBOARD_SVG2 = "M5 9.8V20a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9.8"
COMPANY_SVG = (
    "M4 21V4a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v17M15 9h4a1 1 0 0 1 1 1v11"
    "M3 21h18M8 7h3M8 11h3M8 15h3"
)
CREW_SVG = "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8M4 21a8 8 0 0 1 16 0"
DEVICES_SVG = "M7 2h10a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1M10.5 18h3"
SYSTEM_SVG = "M12 3 4 6v6c0 5 3.4 8.7 8 9 4.6-.3 8-4 8-9V6l-8-3M12 11a2 2 0 1 0 0-4 2 2 0 0 0 0 4M12 11v4"
# A packing box -- lid seam plus the front vertical seam, matching the other
# modules' two/three-stroke visual weight.
FLEET_SVG = "M12 3 4 7l8 4 8-4-8-4M4 7v10l8 4 8-4V7M12 11v10"

# code, name, menu header (blank = no divider), sort order, is_flat, svg, svg2
MODULES = [
    ("general", "General", "", 0, True, "", ""),
    ("company", "Company", "Operations", 10, False, COMPANY_SVG, ""),
    ("crew", "Crew", "Operations", 20, False, CREW_SVG, ""),
    ("devices", "Devices", "Operations", 30, False, DEVICES_SVG, ""),
    ("fleet", "Inventory", "Operations", 40, False, FLEET_SVG, ""),
    ("system", "Users & Roles", "System", 90, False, SYSTEM_SVG, ""),
]

# A page carries its own icon only when its module is flat, i.e. it draws as a
# top-level sidebar link rather than inside a group.
PAGE_ICONS = {
    "general.dashboard": (DASHBOARD_SVG, DASHBOARD_SVG2),
}

# code, module, name, url name, actions, sort order, retired
PAGES = [
    ("general.dashboard", "general", "Dashboard", "dashboard", ["read"], 0, False),

    ("company.country_state", "company", "Country & State", "company-country-state-list", CRUD_PRINT, 1, False),
    ("company.company", "company", "Company", "company-company-list", CRUD_PRINT_APPROVE, 2, False),
    ("company.location", "company", "Location", "company-location-list", CRUD_PRINT, 3, False),
    ("company.department", "company", "Department", "company-department-list", CRUD_PRINT, 4, False),
    ("company.branch", "company", "Branch", "company-branch-list", CRUD_PRINT_APPROVE, 5, False),
    ("company.branch_working_time", "company", "Branch Working Time",
     "company-branch-working-time-edit", ["create", "read", "update"], 6, False),
    ("company.privileges", "company", "Privileges", "company-privileges", ["read", "update"], 9, False),
    # Retired: both edited data that already lives on Branch.
    ("company.branch_address", "company", "Station Address Mapping", "", CRUD, 90, True),
    ("company.branch_department", "company", "Branch Department Mapping", "", CRUD, 91, True),

    ("crew.employee", "crew", "Employee", "crew-employee-list", CRUD_PRINT, 1, False),
    ("crew.designation", "crew", "Designation", "crew-designation-list", CRUD_PRINT, 2, False),
    ("crew.employee_address", "crew", "Employee Address", "crew-address-list", CRUD, 3, False),
    ("crew.block_unblock", "crew", "Block / Unblock", "crew-block-unblock", ["read", "update"], 4, False),
    ("crew.attendance", "crew", "Attendance", "crew-attendance-list", ["read", "print"], 5, False),
    # No "delete": a day is edited by re-saving its week, never removed --
    # the plan a week held is worth keeping even once it is in the past.
    ("crew.duty_roster", "crew", "Duty Roster", "crew-duty-roster-list",
     ["create", "read", "update"], 6, False),
    ("crew.privileges", "crew", "Privileges", "crew-privileges", ["read", "update"], 9, False),

    # Approval has no "create": a device joins the queue by registering itself,
    # and an admin who could create one by hand could invent a station's
    # identity. No "delete" either -- a device is retired, never deleted.
    ("devices.device_approval", "devices", "Device Approval", "devices-device-approval",
     ["read", "update", "approve", "print"], 1, False),
    ("devices.device_mapping", "devices", "Device Mapping", "devices-device-mapping",
     CRUD_PRINT, 2, False),
    # No "delete" on releases: a build is withdrawn, and its rows stay as the
    # record of what each branch ran. Mapping rows are switched off, never
    # deleted, for the same reason.
    ("devices.app_release", "devices", "App Releases", "devices-app-release",
     ["create", "read", "update", "print"], 3, False),
    ("devices.app_release_mapping", "devices", "App Mapping", "devices-app-mapping",
     ["create", "read", "update"], 4, False),
    # No "delete": settings are deactivated, never removed -- bill_continuity
    # copied the prefix, and receipts printed under it are still out there.
    ("devices.device_settings", "devices", "Device Settings", "devices-device-settings",
     ["create", "read", "update", "print"], 5, False),
    ("devices.privileges", "devices", "Privileges", "devices-privileges", ["read", "update"], 9, False),

    ("fleet.brand", "fleet", "Brand", "fleet-brand-list", CRUD_PRINT, 1, False),
    ("fleet.category", "fleet", "Category", "fleet-category-list", CRUD_PRINT, 2, False),
    ("fleet.vehicle_type", "fleet", "Vehicle Type", "fleet-vehicle-type-list", CRUD_PRINT, 3, False),
    ("fleet.uom", "fleet", "UOM", "fleet-uom-list", CRUD_PRINT, 4, False),
    ("fleet.asset_type", "fleet", "Asset Type", "fleet-asset-type-list", CRUD_PRINT, 5, False),
    ("fleet.privileges", "fleet", "Privileges", "fleet-privileges", ["read", "update"], 9, False),

    ("system.role", "system", "Roles", "portal-role-list", CRUD, 1, False),
    ("system.user", "system", "Users", "portal-user-list", CRUD, 2, False),
]
