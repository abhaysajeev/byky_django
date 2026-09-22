"""Drawer specs for the Crew screens.

Ported from the wireframe's apps/byky_hrms/drawers.py. Field ids are kept as
they were -- the screens' filters and JS use them -- and translated to model
fields in apps/crew/writes.py.

Three deliberate differences from the wireframe:

* the Photo field had an empty id there, so it bound to nothing; it has one now;
* Home Country Address is gone from the employee drawer -- an address is a row
  on the Address screen now, and holding it in two places is how they drift;
* every select carries `options_from`, so a dropdown lists this company's rows
  rather than a hardcoded name.
"""

EMPLOYEE = {
    "drawer_id": "drawerEmployee",
    "scr_name": "employee",
    "model": "crew.Employee",
    "add_label": "Add Employee",
    "title_field": "first_name",
    "sections": [
        {
            "title": "Identity",
            "fields": [
                {
                    "id": "company", "label": "Company", "kind": "select",
                    "required": True, "options_from": "companies_list",
                    "option_key": "name", "system_only": True,
                },
                {
                    "id": "emp_no", "label": "Employee Code", "kind": "text",
                    "required": True, "lock_on_edit": True,
                },
                {"id": "photo", "label": "Photo", "kind": "file", "required": False},
                {"id": "first_name", "label": "First Name", "kind": "text", "required": True},
                {"id": "middle_name", "label": "Middle Name", "kind": "text", "required": False},
                {"id": "last_name", "label": "Last Name", "kind": "text", "required": False},
                {
                    "id": "designation", "label": "Designation", "kind": "select",
                    "required": True, "options_from": "designations_list", "option_key": "title",
                },
                {
                    "id": "branch", "label": "Branch", "kind": "select",
                    "required": False, "options_from": "branches_list", "option_key": "name",
                    "help": "The station this person works at.",
                },
                {
                    "id": "attendance_method", "label": "Attendance Method", "kind": "select",
                    "required": False, "options_from": "attendance_methods_list",
                    "option_key": "name",
                    "help": "Leave empty to follow the designation.",
                },
            ],
        },
        {
            "title": "Personal",
            "fields": [
                {"id": "dob", "label": "Date of Birth", "kind": "date", "required": False},
                {
                    "id": "gender", "label": "Gender", "kind": "select", "required": False,
                    "options": ["Male", "Female", "Other"],
                },
                {
                    "id": "marital_status", "label": "Marital Status", "kind": "select",
                    "required": False,
                    "options": ["Single", "Married", "Widowed", "Divorced"],
                },
                {"id": "nationality", "label": "Nationality", "kind": "text", "required": False},
            ],
        },
        {
            "title": "Documents",
            "fields": [
                {"id": "passport_no", "label": "Passport Number", "kind": "text", "required": False},
                {"id": "passport_expiry", "label": "Passport Expiry", "kind": "date", "required": False},
                {"id": "visa_no", "label": "Visa Number", "kind": "text", "required": False},
                {"id": "visa_expiry", "label": "Visa Expiry", "kind": "date", "required": False},
                {"id": "eid_no", "label": "Emirates ID Number", "kind": "text", "required": False},
                {"id": "eid_expiry", "label": "Emirates ID Expiry", "kind": "date", "required": False},
                {"id": "labor_no", "label": "Labour Card Number", "kind": "text", "required": False},
                {"id": "labor_expiry", "label": "Labour Card Expiry", "kind": "date", "required": False},
                {"id": "salary_bank", "label": "Salary Bank", "kind": "text", "required": False},
                {
                    "id": "salary_bank_account", "label": "Salary Account Details",
                    "kind": "text", "required": False,
                },
            ],
        },
        {
            "title": "Contact",
            "fields": [
                {"id": "mobile", "label": "Mobile Number", "kind": "text", "required": False},
                {"id": "email", "label": "Email Address", "kind": "text", "required": False},
                {
                    "id": "emergency_person", "label": "Emergency Contact Person",
                    "kind": "text", "required": False,
                },
                {
                    "id": "emergency_phone", "label": "Emergency Phone",
                    "kind": "text", "required": False,
                },
            ],
        },
    ],
}

DESIGNATION = {
    "drawer_id": "drawerDesignation",
    "scr_name": "designation",
    "model": "crew.Designation",
    "add_label": "Add Designation",
    "title_field": "title",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "company", "label": "Company", "kind": "select",
                    "required": True, "options_from": "companies_list",
                    "option_key": "name", "system_only": True,
                },
                {
                    "id": "code", "label": "Designation Code", "kind": "text",
                    "required": True, "lock_on_edit": True,
                },
                {"id": "title", "label": "Designation Title", "kind": "text", "required": True},
                {"id": "description", "label": "Description", "kind": "textarea", "required": False},
                {
                    "id": "rank", "label": "Rank Order", "kind": "number", "required": False,
                    "help": "Lower is more senior. Leave empty for 1.",
                },
                {
                    "id": "attendance_method", "label": "Attendance Method", "kind": "select",
                    "required": True, "options_from": "attendance_methods_list",
                    "option_key": "name",
                    "help": "How this job's attendance is taken, unless an employee overrides it.",
                },
                {
                    "id": "roster_category", "label": "Duty Roster Category", "kind": "select",
                    "required": False, "options_from": "roster_categories_list",
                    "option_key": "name",
                    "help": "Cashier or Labour, if the Duty Roster staffs this job. Leave "
                            "empty for a designation it never covers (Manager, Supervisor...).",
                },
            ],
        },
    ],
}

ADDRESS = {
    "drawer_id": "drawerAddress",
    "scr_name": "address",
    "model": "crew.EmployeeAddress",
    "add_label": "Add Address",
    "title_field": "line1",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "employee", "label": "Employee", "kind": "select",
                    "required": True, "options_from": "employees_list", "option_key": "name",
                },
                {
                    "id": "address_type", "label": "Address Type", "kind": "select",
                    "required": True, "options_from": "address_types_list", "option_key": "name",
                },
                {"id": "line1", "label": "Address Line 1", "kind": "text", "required": True, "width": 12},
                {"id": "line2", "label": "Address Line 2", "kind": "text", "required": False, "width": 12},
                {"id": "building", "label": "Building Name", "kind": "text", "required": False},
                {"id": "flat", "label": "Flat / Room No", "kind": "text", "required": False},
                {"id": "city", "label": "City", "kind": "text", "required": False},
                {
                    "id": "country", "label": "Country", "kind": "select", "required": False,
                    "options_from": "countries_list", "option_key": "name",
                },
                {
                    "id": "state", "label": "State", "kind": "select", "required": False,
                    "options_from": "states_list", "option_key": "name",
                    "help": "Choose a country first to narrow this list.",
                },
                {"id": "zip_code", "label": "Zip Code", "kind": "text", "required": False},
                {"id": "landlord_name", "label": "Landlord Name", "kind": "text", "required": False},
                {
                    "id": "landlord_phone", "label": "Landlord Contact Phone",
                    "kind": "text", "required": False,
                },
                {"id": "contact_person", "label": "Contact Person Name", "kind": "text", "required": False},
                {"id": "contact_phone", "label": "Contact No", "kind": "text", "required": False},
            ],
        },
    ],
}

# -- Duty Roster (design/duty roster/duty-roster.md) --------------------------
#
# Three drawers, not the wireframe's four: its separate "Add Weekly Branch
# Roster" drawer is dropped -- the Branch Roster tab already is that flow
# (pick a branch, click +Cashier/+Labour per day), so a second drawer
# rendering the identical grid would add nothing. No "state"/"category"
# pre-filter fields either: the employee combo already lists only Cashier and
# Labour staff (roster_eligible_employees), so narrowing by category first
# would filter a list that is already narrow.

DUTY_ROSTER_EMPLOYEE = {
    "drawer_id": "drawerDutyRosterEmployee",
    "scr_name": "duty-roster-employee",
    "model": "crew.DutyRoster",
    "add_label": "Add Employee Roster",
    "title_field": "employee_label",
    "size": "wide",
    "hide_active": True,
    "sections": [
        {
            "title": "Selection",
            "fields": [
                {
                    "id": "employee_key", "label": "Employee", "kind": "combo", "required": True,
                    "combo_source": "roster-employees-data", "combo_key": "name", "combo_sub": "emp_no",
                    "placeholder": "Search employee by name or code...", "width": 12,
                },
                {
                    "id": "week", "label": "Week", "kind": "select", "required": True,
                    "options_from": "roster_week_labels", "width": 12,
                    "help": "This month's weeks. Defaults to the week after the one containing today.",
                },
            ],
        },
        {
            "title": "Data entry — selected week",
            # width 12, not the default 6 -- a custom field defaults to half
            # the drawer's row like any other, and half of "wide" left the
            # day-card grid rendering into a column no wider than a compact
            # drawer's whole body, with dead space beside it.
            "fields": [{"id": "days", "label": "", "kind": "custom", "width": 12}],
        },
    ],
}

DUTY_ROSTER_EDIT_DAY = {
    "drawer_id": "drawerDutyRosterDay",
    "scr_name": "duty-roster-day",
    "model": "crew.DutyRoster",
    "add_label": "Edit Day",
    "title_field": "employee_label",
    "size": "compact",
    "hide_active": True,
    "sections": [
        {
            "title": "",
            "fields": [
                {"id": "employee_key", "label": "", "kind": "custom"},
                {
                    # options_from, not a literal list: the posted value must
                    # be DutyRosterDayType's raw value ("working" etc, what
                    # _check_day matches against), while the option shows its
                    # label. A plain options list would post the label text
                    # itself and every save would fail "Choose a day type."
                    "id": "day_type", "label": "Day Type", "kind": "select", "required": True,
                    "options_from": "roster_day_types", "option_key": "name",
                },
                {
                    "id": "branch", "label": "Branch", "kind": "select",
                    "options_from": "branches_list", "option_key": "name", "show_if": "day_type:working",
                },
                # *_hhmm, not the model's own shift1_start/shift1_end/etc:
                # those are DateTimeFields (the day's date + this time,
                # combined server-side in _check_day), but this box only ever
                # holds a bare "HH:MM" -- a colliding id would make
                # test_date_fields.py demand a date picker on a plain time box.
                {"id": "shift1_start_hhmm", "label": "Shift 1 Start", "kind": "time", "show_if": "day_type:working"},
                {"id": "shift1_end_hhmm", "label": "Shift 1 End", "kind": "time", "show_if": "day_type:working"},
                {"id": "add_shift2", "label": "", "kind": "checkbox", "placeholder": "Add Shift 2 (split shift)", "show_if": "day_type:working"},
                {"id": "shift2_start_hhmm", "label": "Shift 2 Start", "kind": "time", "show_if": "add_shift2:yes"},
                {"id": "shift2_end_hhmm", "label": "Shift 2 End", "kind": "time", "show_if": "add_shift2:yes"},
            ],
        },
    ],
}

DUTY_ROSTER_ADD_TO_DAY = {
    "drawer_id": "drawerDutyRosterAddToDay",
    "scr_name": "duty-roster-add-to-day",
    "model": "crew.DutyRoster",
    "add_label": "Add to Day",
    "title_field": "employee_label",
    "size": "compact",
    "hide_active": True,
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    # Pre-set by the JS to whichever of +Cashier/+Labour was
                    # clicked, and editable -- it exists to narrow the
                    # Employee combo below (combo_filter_*), not to be
                    # posted: a Cashier/Labour split is a fact about the
                    # employee's own designation, already enforced server
                    # side (assign_day), not a value this drawer writes.
                    "id": "category", "label": "Role", "kind": "select", "required": True,
                    "options_from": "roster_categories_list", "option_key": "name",
                },
                {
                    "id": "employee_key", "label": "Employee", "kind": "combo", "required": True,
                    "combo_source": "roster-employees-data", "combo_key": "name", "combo_sub": "emp_no",
                    "combo_filter_field": "category", "combo_filter_key": "role",
                    "placeholder": "Search employee by name or code...",
                },
                # Filled by the JS from whichever day-card's +Cashier/+Labour
                # was clicked -- not asked again, since the click already said
                # which branch and which day. "for_date", not "date": the
                # model's own `date` is a DateField, and this slot renders no
                # picker at all (see the _hhmm note above).
                {"id": "branch", "label": "", "kind": "custom"},
                {"id": "for_date", "label": "", "kind": "custom"},
                {"id": "shift1_start_hhmm", "label": "Shift 1 Start", "kind": "time", "required": True},
                {"id": "shift1_end_hhmm", "label": "Shift 1 End", "kind": "time", "required": True},
                {"id": "add_shift2", "label": "", "kind": "checkbox", "placeholder": "Add Shift 2 (split shift)"},
                {"id": "shift2_start_hhmm", "label": "Shift 2 Start", "kind": "time", "show_if": "add_shift2:yes"},
                {"id": "shift2_end_hhmm", "label": "Shift 2 End", "kind": "time", "show_if": "add_shift2:yes"},
            ],
        },
    ],
}

SPECS = {
    "drawer_employee": EMPLOYEE,
    "drawer_designation": DESIGNATION,
    "drawer_address": ADDRESS,
    "drawer_duty_roster_employee": DUTY_ROSTER_EMPLOYEE,
    "drawer_duty_roster_day": DUTY_ROSTER_EDIT_DAY,
    "drawer_duty_roster_add_to_day": DUTY_ROSTER_ADD_TO_DAY,
}
