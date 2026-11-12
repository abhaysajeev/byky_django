"""The Add / Edit target drawer (theme/templates/byky/partials/drawer.html).

On Add: stations (one or several) and the month. On Edit: only the amount and
remarks change -- the station and month are a target's identity."""

TARGET = {
    "drawer_id": "drawerTarget",
    "scr_name": "target",
    "model": "targets.Target",
    "add_label": "Add Target",
    "title_field": "title",
    "hide_active": True,
    "sections": [
        {
            "title": "",
            "fields": [
                {"id": "branches", "label": "Stations", "kind": "multiselect", "required": True, "width": 12,
                 "options_from": "branches_list", "placeholder": "Select one or more stations"},
                {"id": "month", "label": "Month", "kind": "select", "required": True, "width": 12,
                 "options_from": "months_list", "lock_on_edit": True},
                {"id": "amount", "label": "Target amount", "kind": "number", "required": True, "width": 12,
                 "placeholder": "0.00",
                 "help": "Split evenly over the month's days. Achievement counts orders by booking date."},
                {"id": "remarks", "label": "Remarks", "kind": "textarea", "required": False, "width": 12,
                 "placeholder": "Optional, e.g. Eid season"},
            ],
        },
    ],
}
