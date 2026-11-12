"""The Set / Edit target drawer (theme/templates/byky/partials/drawer.html).

The stations and month come from the page that opened it (Set Targets or a
station's month) and are sent as hidden fields; the person sees them as one
read-only line and only types the amount and remarks."""

TARGET = {
    "drawer_id": "drawerTarget",
    "scr_name": "target",
    "model": "targets.Target",
    "add_label": "Set target",
    "title_field": "title",
    "hide_active": True,
    "size": "compact",
    "sections": [
        {
            "title": "",
            "fields": [
                {"id": "branches", "label": "Branches", "kind": "hidden"},
                {"id": "month", "label": "Month", "kind": "hidden"},
                {"id": "summary", "label": "For", "kind": "text", "readonly": True, "width": 12,
                 "placeholder": " "},
                {"id": "amount", "label": "Target amount", "kind": "number", "required": True, "width": 12,
                 "help": "In AED. Split evenly over the month's days."},
                {"id": "remarks", "label": "Remarks", "kind": "textarea", "required": False, "width": 12,
                 "placeholder": "Optional"},
            ],
        },
    ],
}
