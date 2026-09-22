"""Drawer specs for the Company screens.

A spec is form *structure* -- labels, field kinds, which context key fills a
dropdown -- so it ports across unchanged; only the data behind it is ours.
Rendered by theme/templates/byky/partials/drawer.html and resolved by
theme.drawers.resolve().
"""

BRANCH = {
    "drawer_id": "drawerBranch",
    "scr_name": "branch",
    "model": "company.Branch",
    "add_label": "Add Branch",
    "title_field": "name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "code",
                    "label": "Branch Code",
                    "kind": "text",
                    "required": True,
                    "lock_on_edit": True
                },
                {
                    "id": "name",
                    "label": "Branch Name",
                    "kind": "text",
                    "required": True
                },
                {
                    "id": "company",
                    "label": "Company",
                    "kind": "select",
                    "required": True,
                    "options_from": "companies_list",
                    "option_key": "name",
                    "system_only": True
                },
                {
                    "id": "country",
                    "label": "Country",
                    "kind": "select",
                    "required": True,
                    "options_from": "countries_list",
                    "option_key": "name"
                },
                {
                    "id": "state",
                    "label": "State",
                    "kind": "select",
                    "required": True,
                    "options_from": "states_list",
                    "help": "Choose a country first to narrow this list. Drives which fare plans and RFID gate rules this branch inherits.",
                    "option_key": "name"
                },
                {
                    "id": "location",
                    "label": "Location",
                    "kind": "select",
                    "required": True,
                    "options_from": "locations_list",
                    "option_key": "name"
                },
                {
                    "id": "branch_type",
                    "label": "Branch Type",
                    "kind": "select",
                    "required": True,
                    "options_from": "branch_types_list"
                },
                {
                    "id": "flags",
                    "label": "Branch Flags",
                    "kind": "checkgroup",
                    "required": False,
                    "width": 12,
                    "show_if": "branch_type:Station",
                    "help": "These apply to a station only.",
                    "options": [
                        {"id": "is_hotel", "label": "Is Hotel", "enables": "hotel_commission"},
                        {"id": "app_payment", "label": "Is App Payment"},
                        {"id": "multi_user", "label": "Allow Multiple Devices"},
                        {"id": "test_vehicle", "label": "Is Test Vehicle"}
                    ]
                },
                {
                    "id": "hotel_commission",
                    "label": "Hotel Commission %",
                    "kind": "number",
                    "required": False,
                    "help": "Enabled once Is Hotel is switched on."
                },
                {
                    "id": "departments",
                    "label": "Departments",
                    "kind": "multiselect",
                    "required": False,
                    "width": 12,
                    "placeholder": "Select departments",
                    "options_from": "departments_list",
                    "option_key": "name",
                    "empty_text": "No departments have been added yet — add them in Department Master and they will appear here.",
                    "help": "A branch can run several departments; each one you pick stays visible as a chip."
                },
                {
                    "id": "station_no",
                    "label": "Station No",
                    "kind": "text",
                    "required": False,
                    "help": "The station's own number, distinct from the branch code."
                },
                {
                    "id": "address",
                    "label": "Address",
                    "kind": "textarea",
                    "required": False,
                    "width": 12
                },
                {
                    "id": "latitude",
                    "label": "Latitude",
                    "kind": "number",
                    "required": False,
                    "help": "-90 to +90"
                },
                {
                    "id": "longitude",
                    "label": "Longitude",
                    "kind": "number",
                    "required": False,
                    "help": "-180 to +180"
                },
                {
                    "id": "contact_no",
                    "label": "Contact No",
                    "kind": "text",
                    "required": False
                },
                {
                    "id": "image",
                    "label": "Branch Image",
                    "kind": "file",
                    "required": False,
                    "width": 12
                }
            ]
        },
        {
            "title": "Approval Authority",
            "fields": [
                {
                    "id": "leave_request_authority",
                    "label": "Leave Request",
                    "kind": "multiselect",
                    "required": False,
                    "width": 12,
                    "placeholder": "Select approvers",
                    "options_from": "employees_list",
                    "option_key": "name",
                    "help": "Staff at this branch who can approve leave requests."
                },
                {
                    "id": "maintenance_authority",
                    "label": "Service & Maintenance",
                    "kind": "multiselect",
                    "required": False,
                    "width": 12,
                    "placeholder": "Select approvers",
                    "options_from": "employees_list",
                    "option_key": "name",
                    "help": "Staff at this branch who can approve service & maintenance requests."
                },
                {
                    "id": "rms_app_authority",
                    "label": "RMS App Request",
                    "kind": "multiselect",
                    "required": False,
                    "width": 12,
                    "placeholder": "Select approvers",
                    "options_from": "employees_list",
                    "option_key": "name",
                    "help": "Staff at this branch who can approve RMS app requests."
                }
            ]
        }
    ]
}

COUNTRY = {
    "drawer_id": "drawerCountry",
    "scr_name": "country",
    "model": "company.Country",
    "add_label": "Add Country",
    "title_field": "name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "code",
                    "label": "Country Code",
                    "kind": "text",
                    "required": True,
                    "lock_on_edit": True
                },
                {
                    "id": "name",
                    "label": "Country Name",
                    "kind": "text",
                    "required": True
                }
            ]
        }
    ]
}

DEPARTMENT = {
    "drawer_id": "drawerDepartment",
    "scr_name": "department",
    "model": "company.Department",
    "add_label": "Add Department",
    "title_field": "name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "company",
                    "label": "Company",
                    "kind": "select",
                    "required": True,
                    "options_from": "companies_list",
                    "option_key": "name",
                    "system_only": True
                },
                {
                    "id": "code",
                    "label": "Department Code",
                    "kind": "text",
                    "required": True
                },
                {
                    "id": "name",
                    "label": "Department Name",
                    "kind": "text",
                    "required": True
                }
            ]
        }
    ]
}

LOCATION = {
    "drawer_id": "drawerLocation",
    "scr_name": "location",
    "model": "company.Location",
    "add_label": "Add Location",
    "title_field": "name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "code",
                    "label": "Location Code",
                    "kind": "text",
                    "required": True
                },
                {
                    "id": "name",
                    "label": "Location Name",
                    "kind": "text",
                    "required": True
                },
                {
                    "id": "country",
                    "label": "Country",
                    "kind": "select",
                    "required": True,
                    "options_from": "countries_list",
                    "option_key": "name"
                },
                {
                    "id": "state",
                    "label": "State",
                    "kind": "select",
                    "required": True,
                    "options_from": "states_list",
                    "help": "Choose a country first to narrow this list.",
                    "option_key": "name"
                },
                {
                    "id": "landmark",
                    "label": "Landmark",
                    "kind": "text",
                    "required": False
                }
            ]
        }
    ]
}

STATE = {
    "drawer_id": "drawerState",
    "scr_name": "state",
    "model": "company.State",
    "add_label": "Add State",
    "title_field": "name",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "code",
                    "label": "State Code",
                    "kind": "text",
                    "required": True,
                    "lock_on_edit": True
                },
                {
                    "id": "name",
                    "label": "State Name",
                    "kind": "text",
                    "required": True
                }
            ]
        }
    ]
}

SPECS = {

    "drawer_branch": BRANCH,

    "drawer_country": COUNTRY,

    "drawer_department": DEPARTMENT,

    "drawer_location": LOCATION,

    "drawer_state": STATE,

}


# The Company form, as field structure. Keys are our model's field names, so
# one definition drives the drawer, the completeness percentage and the row's
# prefill payload.
COMPANY_SECTIONS = [
    ("basic", "Basics", [
        ("Company Code", "short_code", True, "Immutable once approved.", 1),
        ("Company Name", "name", True, "", 2),
        ("CEO Name", "ceo_name", False, "", 1),
        ("DTO Name", "dto_name", False, "", 1),
        ("Date of Commissioning", "date_of_commissioning", False, "", 1),
    ]),
    ("address", "Address", [
        ("Address", "address", False, "", 2),
        ("State", "state", True, "", 1),
        ("City", "city", False, "", 1),
        ("ZIP Code", "zip_code", False, "", 1),
    ]),
    ("tax", "Registration", [
        ("Incorporation Certificate No", "incorporation_certificate_number", False, "", 1),
        ("Business Certificate No", "business_certificate_number", False, "", 1),
        ("Income Tax No", "income_tax_number", False, "", 1),
        ("Tax %", "tax_percentage", False, "UAE standard VAT is 5%.", 1),
        ("TIN", "tin", False, "", 1),
        ("CST", "cst", False, "", 1),
        ("Service Tax No", "service_tax_number", False, "", 1),
    ]),
    ("contact", "Contact", [
        ("Contact Person", "contact_person", False, "", 1),
        ("Phone Number", "phone_number", True, "", 1),
        ("Fax", "fax_number", False, "", 1),
        ("Email Address", "email", True, "", 1),
        ("Web Address", "website", False, "", 1),
    ]),
]


def company_sections(company=None):
    """The form's four sections, with values read off a Company instance.

    Passing None gives the same structure with empty values -- what the Add
    drawer needs, and what an empty database renders.
    """
    sections = []
    for key, label, fields in COMPANY_SECTIONS:
        out = []
        for flabel, fkey, required, help_text, span in fields:
            value = ""
            if company is not None:
                raw = getattr(company, fkey, "")
                if fkey == "state":
                    raw = company.state.name if company.state_id else ""
                value = "" if raw is None else raw
            out.append({
                "key": fkey,
                "label": flabel,
                "value": value,
                "required": required,
                "help": help_text,
                "span": span,
            })
        sections.append({"key": key, "label": label, "fields": out})
    return sections


def company_spec(form_sections=None):
    """Build the Company drawer from COMPANY_SECTIONS.

    This is the one drawer whose fields are generated rather than written out,
    so the drawer and the record it edits can never drift apart. Every field is
    a plain text input except State and the drawer-only Country select.
    """
    form_sections = form_sections or company_sections()
    sections = []
    for i, s in enumerate(form_sections):
        fields = []
        if i == 0:
            fields.append({
                "id": "logo", "label": "Company Logo", "kind": "file",
                "required": False,
                "help": "PNG, JPG or SVG · square, at least 256×256px.",
            })
        if s["key"] == "address":
            # Drawer-only field: not part of the 20-field FSD 1.1 record
            # (data.company() has no "country" key), so it stays out of
            # _COMPANY_SECTIONS / company_completeness() and is injected
            # here, same as Logo above.
            fields.append({
                "id": "country", "label": "Country", "kind": "select",
                "required": False,
                "options_from": "countries_list", "option_key": "name",
                "default": "United Arab Emirates",
                "width": 6,
            })
        for f in s["fields"]:
            if f["key"] == "state":
                fields.append({
                    "id": "state",
                    "label": "State",
                    "kind": "select",
                    "required": f["required"],
                    "options_from": "states_list",
                    "option_key": "name",
                    "default": "Dubai",
                    "help": f.get("help") or "",
                    "width": f.get("span") or 6,
                })
                continue
            fields.append({
                "id": f["key"],
                "label": f["label"],
                "kind": "text",
                "required": f["required"],
                "placeholder": f["label"],
                "help": f.get("help") or "",
                "lock_on_edit": f["key"] == "code",
                "width": f.get("span") or 6,
            })
        sections.append({"title": s["label"], "fields": fields})
    return {
        "drawer_id": "drawerCompany",
        "scr_name": "company",
    "model": "company.Company",
        "add_label": "Add Company",
        "title_field": "name",
        "sections": sections,
    }
