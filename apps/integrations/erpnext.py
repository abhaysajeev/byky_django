"""The ERPNext side of the contract, as `byky_app/rms_sync.py` sends it
(design/10-erpnext-integration.md §4, §14; the developer's code of 23 Nov 2026
is kept in byky-f/analysis/erpnext/2026-11-23/).

Each DocType posts to `<Base URL>/<slug>`; the slugs are theirs.
"""

# slug -> the ERPNext DocType that posts to it
SLUGS = {
    "country": "Country",
    "state": "State",
    "branch-type": "Branch Type",
    "branch": "Branch",
    "event-location": "Event Location",
    "warehouse": "Warehouse",
    "designation": "Designation",
    "employee": "Employee",
    "department": "Department",
    "brand": "Brand",
    "item-group": "Item Group",
    "item": "Item",
    "vehicle": "Serial No",
    "asset": "Asset",
}
DOCTYPES = sorted(set(SLUGS.values()))

ACTIONS = ("create", "update", "deactivate")

# Their envelope (rms_sync.build_payload): every key a message carries.
ENVELOPE_KEYS = ("event_id", "doctype", "name", "action", "modified", "sent_at", "company", "data")

# Personal data: a request-log row keeps only a summary of these bodies.
PERSONAL_SLUGS = ("employee",)

API_PREFIX = "/api/v1/integrations/erpnext/"
