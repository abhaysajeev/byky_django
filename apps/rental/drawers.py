"""Drawer specs for the Rental Management screens.

Field ids match the Customer model's own field names throughout, so no
translation map is needed between the drawer's payload and CustomerForm
(unlike crew's drawer, whose ids were ported from the wireframe verbatim).
"""

CUSTOMER = {
    "drawer_id": "drawerCustomer",
    "scr_name": "customer",
    "model": "rental.Customer",
    "add_label": "Add Customer",
    "title_field": "first_name",
    "size": "wide",
    "sections": [
        {
            "title": "Personal",
            "fields": [
                {
                    "id": "company", "label": "Company", "kind": "select",
                    "required": True, "options_from": "companies_list",
                    "option_key": "name", "system_only": True,
                },
                {"id": "first_name", "label": "First Name", "kind": "text", "required": True},
                {"id": "last_name", "label": "Last Name", "kind": "text", "required": False},
                {
                    "id": "gender", "label": "Gender", "kind": "select", "required": False,
                    "options_from": "genders_list", "option_key": "name",
                },
                {"id": "date_of_birth", "label": "Date of Birth", "kind": "date", "required": False},
                {"id": "nationality", "label": "Nationality", "kind": "text", "required": False},
            ],
        },
        {
            "title": "Identification",
            "fields": [
                {
                    "id": "id_type", "label": "ID Type", "kind": "select", "required": False,
                    "options_from": "id_types_list", "option_key": "name",
                },
                {
                    "id": "id_no", "label": "Document No", "kind": "text", "required": False,
                    "help": "Not mandatory -- a walk-in customer can be registered before it's captured.",
                },
            ],
        },
        {
            "title": "Contact",
            "fields": [
                {
                    "id": "mobile_country_code", "label": "Country Code", "kind": "text",
                    "required": True, "placeholder": "+971", "width": 4,
                },
                {"id": "mobile_no", "label": "Phone No", "kind": "text", "required": True, "width": 8},
                # Shown, never typed: the model builds it from the two fields
                # above, and byky-customer.js previews it as they are typed.
                {"id": "mobile_full", "label": "Full Number", "kind": "text", "required": False, "readonly": True,
                 "placeholder": "Country code and number, digits only"},
                {"id": "email", "label": "Email", "kind": "text", "required": False},
                {"id": "remarks", "label": "Remarks", "kind": "textarea", "required": False},
                {"id": "address", "label": "Address", "kind": "textarea", "required": False, "width": 12},
            ],
        },
        {
            "title": "Status",
            "fields": [
                {
                    "id": "is_blocked", "label": "Blocked", "kind": "checkbox",
                    "enables": "block_reason",
                },
                {
                    "id": "block_reason", "label": "Block Reason", "kind": "text",
                    "required": False, "show_if": "is_blocked:yes", "width": 12,
                },
                # The record's state when opened, so the unblock reason shows
                # only when a blocked customer is being unblocked.
                {"id": "was_blocked", "label": "", "kind": "hidden"},
                {
                    "id": "unblock_reason", "label": "Unblock Reason", "kind": "text",
                    "required": False, "show_if": "was_blocked:yes&is_blocked:no", "width": 12,
                },
            ],
        },
    ],
}

PAYMENT_MODE = {
    "drawer_id": "drawerPaymentMode",
    "scr_name": "payment_mode",
    "model": "company.PaymentMode",
    "add_label": "Add Payment Mode",
    "title_field": "name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "company", "label": "Company", "kind": "select",
                    "required": True, "options_from": "companies_list",
                    "option_key": "name", "system_only": True,
                },
                {"id": "name", "label": "Payment Mode", "kind": "text", "required": True},
            ],
        }
    ],
}

# Suspense Entries (owner, 26 Nov 2026; apps/rental/suspense.py). Suspected
# orders are optional and change with the branch and date: byky-suspense.js
# reloads them (BykyMulti.setOptions) from rental-suspense-orders.
SUSPENSE = {
    "drawer_id": "drawerSuspense",
    "scr_name": "suspense",
    "model": "rental.SuspenseEntry",
    "add_label": "Add Suspense Entry",
    "title_field": "title",
    "hide_active": True,
    "sections": [
        {
            "title": "Collection",
            "fields": [
                {"id": "branch", "label": "Branch", "kind": "select", "required": True,
                 "options_from": "suspense_branches_list", "option_key": "name", "width": 12},
                {"id": "entry_date", "label": "Date", "kind": "date", "required": True,
                 "help": "The collection's business day."},
                {"id": "kind", "label": "Type", "kind": "select", "required": True,
                 "options_from": "suspense_kinds_list", "option_key": "name"},
                {"id": "amount", "label": "Amount (AED)", "kind": "number", "required": True, "width": 12,
                 "help": "Always positive. The type says whether it is short or excess."},
            ],
        },
        {
            "title": "Details",
            "fields": [
                {"id": "suspected_orders", "label": "Suspected orders", "kind": "multiselect", "width": 12,
                 "options_from": "suspense_no_orders", "placeholder": "Optional: pick one or more orders",
                 "searchable": True, "search_label": "Search order no",
                 "empty_text": "Pick a branch and a date to list its orders.",
                 "help": "Leave empty if you do not know which order it was."},
                {"id": "remarks", "label": "Remarks", "kind": "textarea", "width": 12},
            ],
        },
    ],
}

SPECS = {"drawer_customer": CUSTOMER, "drawer_payment_mode": PAYMENT_MODE, "drawer_suspense": SUSPENSE}
