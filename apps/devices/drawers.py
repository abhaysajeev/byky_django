"""Drawer specs for the Device screens.

Structure only -- labels, field kinds, which context key fills a dropdown --
rendered by theme/templates/byky/partials/drawer.html.

The wireframe's drawer carried three read-only mirror fields (Device ID, Device
Name, MAC Address) that its own JavaScript filled from a demo blob when the
picker changed. They are not reproduced: there is no MAC in this design at all,
and the picker's own label already carries the registration number and the name,
so the mirrors would be three empty boxes waiting on a script. Recorded in
PORTING.md.

No date field either: mapping is stamped when it is saved, as the legacy did.
"""

DEVICE_MAPPING = {
    "drawer_id": "drawerDeviceMapping",
    "scr_name": "device_mapping",
    "model": "devices.DeviceMapping",
    "add_label": "Add Device Mapping",
    "title_field": "device",
    "sections": [
        {
            "title": "",
            "fields": [
                {
                    "id": "device",
                    "label": "Device",
                    "kind": "select",
                    "required": True,
                    "options_from": "unmapped_devices_list",
                    "help": (
                        "Only approved or blocked devices with no station are "
                        "listed. To move a mapped device, use Re-map."
                    ),
                },
                {
                    "id": "branch",
                    "label": "Station",
                    "kind": "select",
                    "required": True,
                    "options_from": "branches_list",
                    "help": (
                        "A station that does not allow multiple devices can "
                        "hold only one at a time."
                    ),
                },
            ],
        }
    ],
}

# One APK build. What it says is what the update-check reply carries
# (design/registration/update-check-api.md section 4): version_code,
# version_name, update_note, download_url and, from the update type,
# is_mandatory. App, name and code lock on edit -- tablets compare against the
# code, and changing it under them would re-decide every tablet silently.
# No Active switch: a release is withdrawn and restored from its row menu, with
# a confirm that says where its branches go.
APP_RELEASE = {
    "drawer_id": "drawerAppRelease",
    "scr_name": "app_release",
    "model": "devices.AppRelease",
    "add_label": "Add Release",
    "title_field": "title",
    "hide_active": True,
    "sections": [
        {
            "title": "Build",
            "note_from": "release_company_note",
            "fields": [
                {
                    "id": "company",
                    "label": "Company",
                    "kind": "select",
                    "required": True,
                    "lock_on_edit": True,
                    "width": 12,
                    "system_only": True,
                    "options_from": "release_companies",
                    "help": "One server, several companies. A release belongs to one of them.",
                },
                {
                    "id": "channel",
                    "label": "App",
                    "kind": "select",
                    "required": True,
                    "lock_on_edit": True,
                    "options_from": "release_apps",
                },
                {
                    "id": "version_name",
                    "label": "Version name",
                    "kind": "text",
                    "required": True,
                    "lock_on_edit": True,
                    "placeholder": "1.12.81",
                    "help": "Three numbers with dots. Shown to people on the tablet.",
                },
                {
                    "id": "version_code",
                    "label": "Version code",
                    "kind": "number",
                    "required": True,
                    "lock_on_edit": True,
                    "width": 12,
                    "help": (
                        "The build's versionCode from Android. Tablets send the "
                        "code they have installed and compare it with this one, "
                        "so it must be higher than the app's last release."
                    ),
                },
            ],
        },
        {
            "title": "Update",
            "fields": [
                {
                    "id": "update_type",
                    "label": "Update type",
                    "kind": "select",
                    "required": True,
                    "default": "anytime",
                    "width": 12,
                    "options_from": "release_update_types",
                    "help": (
                        "Mandatory: the tablet must install it before anyone can "
                        "log in. Any time: offered with Update and Later, and only "
                        "while the tablet's branch is open (Branch Working Time)."
                    ),
                },
                {
                    "id": "download_url",
                    "label": "Download link",
                    "kind": "text",
                    "required": True,
                    "width": 12,
                    "placeholder": "Link to the APK on the file server",
                },
                {
                    "id": "update_note",
                    "label": "What's new",
                    "kind": "textarea",
                    "width": 12,
                    "help": "Optional. Shown on the tablet with the update.",
                },
            ],
        },
    ],
}

# How one station's tablets print and price (design/03-login.md section 8.7).
# The station is fixed once saved -- the row is what that station prints -- so
# it locks on edit. No Active switch: settings are deactivated from the row
# menu, which says what happens to the tablets. The logo has its own upload:
# a file cannot ride inside the JSON this drawer posts.
DEVICE_SETTINGS = {
    "drawer_id": "drawerDeviceSettings",
    "scr_name": "device_settings",
    "model": "devices.DeviceSettings",
    "add_label": "Add Device Settings",
    "title_field": "title",
    "hide_active": True,
    "sections": [
        {
            "title": "Station",
            "note_from": "settings_company_note",
            "fields": [
                {
                    "id": "copy_from",
                    "label": "Copy from another station",
                    "kind": "select",
                    "width": 12,
                    "options_from": "settings_sources",
                    "help": (
                        "Optional. Fills the wording and printing below from that "
                        "station, so one footer does not end up written five ways. "
                        "Station, code and prefix stay yours to enter."
                    ),
                },
                {
                    "id": "branch",
                    "label": "Station",
                    "kind": "select",
                    "required": True,
                    "lock_on_edit": True,
                    "options_from": "settings_branches",
                    "option_key": "label",
                    "help": "A station has one set of settings at a time.",
                },
                {
                    "id": "settings_code",
                    "label": "Settings Code",
                    "kind": "text",
                    "required": True,
                    "placeholder": "creek1",
                },
                {
                    "id": "order_no_prefix",
                    "label": "Order No Starting Characters",
                    "kind": "text",
                    "required": True,
                    "placeholder": "DUBPP",
                    "help": (
                        "Up to 5 letters or digits. Every receipt number here starts "
                        "with it, then the tablet's number: DUBPP60182000335. Two "
                        "stations cannot share one."
                    ),
                },
            ],
        },
        {
            "title": "Receipt text",
            "fields": [
                {
                    "id": "header_1",
                    "label": "Header 1",
                    "kind": "text",
                    "required": True,
                    "placeholder": "CREEK PARK 1",
                    "help": "The station, as the receipt should name it. Filled from the station you pick.",
                },
                {"id": "header_2", "label": "Header 2", "kind": "text", "required": True,
                 "placeholder": "DUBAI"},
                {"id": "additional_header_1", "label": "Additional Header 1", "kind": "text", "width": 12,
                 "placeholder": "BYKY SPORT & LEISURE EQUIPMENT RENTAL & TRADING LLC",
                 "help": "The legal name printed under the station. It follows the licence the station trades under."},
                {"id": "additional_header_2", "label": "Additional Header 2", "kind": "text", "width": 12,
                 "placeholder": "Optional"},
                {"id": "footer_1", "label": "Footer 1", "kind": "text", "required": True, "width": 12,
                 "placeholder": "Company is not responsible for personal belongings"},
                {"id": "footer_2_arabic", "label": "Footer 2 (Arabic)", "kind": "text", "width": 12,
                 "placeholder": "الشركة غير مسؤولة عن فقدان الأغراض الشخصية"},
            ],
        },
        {
            "title": "Printing",
            "fields": [
                {"id": "paper_feed", "label": "Paper Feed", "kind": "number", "required": True,
                 "help": "Blank lines fed after a receipt so it tears off cleanly. 0 to 10."},
                {"id": "receipt_copies", "label": "No of Receipt Copy", "kind": "number", "required": True,
                 "help": "How many copies print per rental. 1 to 10."},
                {"id": "print_type", "label": "Print Type", "kind": "select", "required": True,
                 "default": "landscape", "options_from": "print_types"},
                {"id": "print_logo", "label": "Print Logo", "kind": "checkbox",
                 "placeholder": "Print the station's logo on the receipt", "checked": True},
                {"id": "share_on_whatsapp", "label": "Share on WhatsApp", "kind": "checkbox", "width": 12,
                 "placeholder": "Offer to send the receipt on WhatsApp"},
            ],
        },
        {
            "title": "Pricing and test rides",
            "fields": [
                {"id": "round_off_mode", "label": "Round Off Type", "kind": "select", "required": True,
                 "default": "upward", "options_from": "round_off_modes"},
                {"id": "round_off_step", "label": "Round Off Limit", "kind": "select", "required": True,
                 "default": "0.25", "options_from": "round_off_steps"},
                {"id": "customer_test_minutes", "label": "Customer Test Time Slot (Min)", "kind": "number",
                 "required": True, "help": "How long a customer may try a vehicle before it counts as a rental."},
                {"id": "cashier_test_minutes", "label": "Cashier Test Time Slot (Min)", "kind": "number",
                 "required": True, "help": "The same, for a cashier checking a vehicle."},
            ],
        },
    ],
}

SPECS = {
    "drawer_device_mapping": DEVICE_MAPPING,
    "drawer_app_release": APP_RELEASE,
    "drawer_device_settings": DEVICE_SETTINGS,
}
