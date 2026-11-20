"""The Edit-this-period drawer (theme/templates/byky/partials/drawer.html).

Opened from a target's page: the branch, profile and period show as one
read-only line; the person only types the amount and remarks. Saving marks
the period as set on its own (design/11-target-profiles.md §5)."""

TARGET = {
    "drawer_id": "drawerTarget",
    "scr_name": "target",
    "model": "targets.Target",
    "add_label": "Edit this period",
    "title_field": "title",
    "hide_active": True,
    "size": "compact",
    "sections": [
        {
            "title": "",
            "fields": [
                {"id": "branches", "label": "Branches", "kind": "hidden"},
                {"id": "month", "label": "Month", "kind": "hidden"},
                {"id": "kind", "label": "Kind", "kind": "hidden"},
                {"id": "summary", "label": "For", "kind": "text", "readonly": True, "width": 12,
                 "placeholder": " "},
                {"id": "amount", "label": "Target amount", "kind": "number", "required": True, "width": 12,
                 "help": "For this period only; a later change to the profile's amount leaves it alone. Sales: AED. Customers: a whole number."},
                {"id": "remarks", "label": "Remarks", "kind": "textarea", "required": False, "width": 12,
                 "placeholder": "Optional"},
            ],
        },
    ],
}
