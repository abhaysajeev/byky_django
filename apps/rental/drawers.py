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
            ],
        },
    ],
}

SPECS = {"drawer_customer": CUSTOMER}
