# Porting ledger

One line per screen ported from the wireframe
(`~/Desktop/2026/byky-main/django-version/full-version`) into this project, per
`.claude/skills/port-ui/SKILL.md`.

The **Differences** column carries every deliberate departure from the reference
screen. A silent difference is a defect; a recorded one is a decision.

## Models (no screens yet)

| Area | Source | Status | Date |
|---|---|---|---|
| Company module models | `design/02-company.md`, `company_schema_day1.pdf` | done | 2026-09-18 |
| Portal / RBAC models + services | `design/rbac.md`, `rbac_schema.pdf` | done | 2026-09-18 |
| Login: session, sign-in rules, web views | `design/03-login.md` | done (web) | 2026-09-18 |
| Device models: `Device` rev 5, `DeviceMapping`, `DeviceStatusLog` | `design/03-login.md` §8.3/§8.6, `design/registration/registration-api.md` | done | 2026-09-18 |

## Screens

| Page code | Screen | Reference template | Status | Date | Differences |
|---|---|---|---|---|---|
| `general.dashboard` | Dashboard | `apps/byky_core/templates/byky_dashboard.html` | done | 2026-09-18 | Every widget kept. Charts receive **empty arrays, not zeros** — the chart code divides by the series maximum and all-zero data draws NaN geometry. "Indicative" chips, the revenue-seasonality sentence and the indicative-figures footnote removed: there are no figures to caveat. Hardcoded `+6.4%`, `94%/6%` and the three progress widths became context values. Station map, top stations, workforce and agreements gained `{% empty %}` states. `cms-station-address-mapping` link repointed. |
| `company.company` | Company Details | `cms_company_details.html` | done | 2026-09-18 | `form_sections` rebuilt from our model's field names; completeness computed from filled fields. The two commented-out "Add Company" buttons left as they were. |
| `company.country_state` | Country & State | `cms_country_state_management.html` | done | 2026-09-18 | All five KPI tiles kept. Total Vehicles / Total Assets read `0` and their links are inert until the fleet module exists. Kuwait no longer filtered out — that was a data decision, and the data is gone. |
| `company.location` | Location | `cms_location_management.html` | done | 2026-09-18 | Empty-state copy rewritten: no "once the client supplies…". |
| `company.department` | Department | `cms_department_management.html` | done | 2026-09-18 | Same empty-state rewrite. |
| `company.branch` | Branch | `cms_branch_management.html` | done | 2026-09-18 | **Branch type tiles cut from five to three** (Head Office / Depot / Station) — `design/02-company.md` defines three, and the design doc wins over the markup. Assets and Vehicles tiles read `0`. The per-branch vehicle modal is kept and opens empty: no vehicle model yet. |
| `company.branch_working_time` | Branch Working Time | `cms_station_working_time.html` | done (rewired 19 Sep 2026) | 2026-09-18 | `{{ purpose }}` (FSD metadata) replaced with a literal. **Correction:** the first port claimed the fabricated `04:00–03:59` default was removed and the matrix built from `BranchWorkingTime` — neither was true; the template still hardcoded both times, looped the raw day tuples and had no save handler. Rewired 19 Sep 2026, see below. KPI tiles, the Load button and the hint line removed at the client's request. |
| `company.branch_department` | Branch Department Mapping | `cms_branch_department_mapping.html` | done | 2026-09-18 | Grid now loops the real `Branch.departments` M2M instead of a hardcoded empty row. Leave-authority columns render blank: `BranchApprovalAuthority` lands with the crew module. |
| `company.branch_address` | Station Address Mapping | `cms_station_address_mapping.html` | done | 2026-09-18 | Map points come from `Branch` lat/long, cast to `float` (a `Decimal` is not JSON serialisable). Empty map frame when no branch has coordinates. |
| `devices.device_approval` | Device Approval | `device_device_approval.html` + `_detail.html` | done | 2026-09-18 | **MAC Address column → Device ID + Model/Platform**: there is no MAC in this design, and Android has not given apps the real one since 6.0 (`01-auth.md` §1.1). **Attempted Username column and the detail page's "Login Attempt" section dropped** — registration runs before login and carries no username (`registration-api.md` §4), so nothing could fill them. **APK Version → Last Seen**: the version arrives at login, on `AppSession.app_version`, not at registration. Detail route keyed on `device_registration_id`, not the wireframe's `<str:mac>` — that number is what the admin is told over the phone. A pending row matched as a reinstall shows the "looks like …" hint from `reconnect_of`. The legacy's licence gate on approve (`DeviceController.cs:234`) is not carried: licences are out of scope (`03-login.md` open item 9). Actions render and are permission-gated but do nothing yet. |
| `devices.device_mapping` | Device Mapping | `device_device_mapping.html` | done | 2026-09-18 | **MAC Address column → Model**. App Version, Last Login, Employee and Status read from `AppSession` (latest session per device, `DISTINCT ON`), which is what the legacy computed too — its Status icon was not `IsActive` but "`ToDate IS NULL AND LoginTime IS NOT NULL AND LogoutTime IS NULL`". Status labels are In use / Idle / Blocked. **From Date now comes from a real `DeviceMapping` row**, so a re-map is visible. The drawer's three read-only mirror fields (Device ID / Name / MAC) are dropped — the wireframe filled them from a demo blob with its own JS, and the picker's label already carries the number and name; a `from_date` field is added instead. Re-map and Close mapping are disabled while the device has an open session, which is the legacy's "Can't edit in Active Status". Mapping-level Approve / Reject columns dropped: approval lives on the device (`03-login.md` decision 20, §8.6). Odometer fields not carried — hidden in the legacy UI and always 0. |
| — | Device Settings, Upload APK | `device_device_settings.html`, `device_upload_apk.html` | not ported | 2026-09-18 | Settings is the legacy `SfaDeviceSetting` (per-branch printer/receipt config) and has no model yet. Upload APK is `app_release` / `app_release_branch`, which still carry the pre-rev-5 shape; it lands with the App Versions screen (`03-login.md` §9A.6). |

## Shell and cross-cutting

| Thing | Difference |
|---|---|
| **Sidebar** | Built from `Page` rows filtered by the role's permissions, **not** `vertical_menu.json`. The templates are unchanged; `theme/menu.py` produces the same structure. A page whose route does not reverse is skipped, or one bad row would take down every page. Icons seeded from the wireframe JSON into `Module.svg` / `Page.svg`. |
| **Menu contents** | Lists Station Address Mapping and Branch Department Mapping, which the wireframe's menu omitted although both were routed. An unreachable built screen is a defect. |
| **Navbar** | Hardcoded "Admin / Administrator / AD" now show the signed-in user, their role and computed initials. Log Out wired to `logout`. The alerts-list and Configuration links removed — they point at modules not built; they return with those modules. |
| **Alert feed** | Empty list from `theme/context_processors.py`. The wireframe's feed read its own alerts module. |
| **`TemplateLayout`** | The wireframe imported `templates.layout.bootstrap.<layout>` from a runtime-built dotted path. Replaced with a plain dict: one layout, no import magic, no dependency on the templates directory being a package. |
| **Template tags** | Every filter that called `django.contrib.auth` (`has_group`, `is_admin`, `is_staff`, `is_superuser`, …) dropped; `has_permission` now reads our own permission model. `filter_by_url` hardened against `resolver_match` being None on error pages. |
| **Assets** | 5.2 MB of the wireframe's 64 MB, paths preserved so no `{% static %}` string changed. Excluded and verified unused: flag icons (5.4 MB), FontAwesome (1 MB), audio, demo JSON and every image folder but branding and favicon. |
| **Inline styles / hex in `.scr-*` and `.bd-*` markup** | Kept. These are the approved design system copied verbatim; editing them would break "visually identical". Same standing exception the sidebar files already have. |
| **Forms** | Drawers open and prefill but save nothing. Writes arrive with the CRUD pass. |
| **Action buttons** | Add, Export, Edit and Delete are wrapped in `{% if perm.<action> %}`, fed by `RolePermission` — never an "is admin" check. The legacy decided this with `RoleID != 1` in C#, so changing who could create anything meant a deploy. The two Add Company buttons the wireframe commented out are back: multi-company is a confirmed requirement. |
| **Static storage in tests** | Production keeps WhiteNoise's manifest storage; tests use the plain backend (`conftest.py`), and `collectstatic` is what proves the asset paths exist. |

## Multi-company scoping (added after the port)

| Thing | Decision |
|---|---|
| **Two scopes** | `user.scope` is `company` (sees one company) or `system` (sees every company). An explicit field, not "company is empty" — a user created without a company must see nothing, not everything. Two database constraints hold the pair together. |
| **One filter** | Every list goes through `core.scoping.scoped_to`. The documented way shared-database multi-tenancy leaks is a query that forgets its filter, so there is one filter to get right, and it is tested on every screen. |
| **Fails closed** | A company user with no company gets an empty queryset, never an unfiltered one. |
| **Country, State, Location** | Stay global — the UAE is the UAE, and copying it per company is how a system ends up with three spellings of one emirate. For a company user they are **derived**: the countries its branches sit in, plus its own registered country. |
| **`Department.company_id`** | Added (revision 5 of `design/02-company.md`). The first pass made a department's name unique system-wide, so two companies could not each run an "Operations" department. |
| **`Role.company` nullable** | An empty company means a system role, held only by system users. Permissions are still checked — scope decides which rows are visible, never which actions are allowed. |
| **Promoting a user** | `manage.py set_user_scope --username admin --scope system`. Who holds it is one query: `SELECT username FROM "user" WHERE scope = 'system'`. |
| **Drawer fields** | A field marked `system_only` is shown only to system users — a company user never picks a company, because it is theirs and the server fills it in. Offering that choice is both noise and a way to write into another company's data. |
| **Demo data removed from the drawers** | The Branch drawer hardcoded `"options": ["BYKY"]` for Company, and three approval-authority selects listed two real people by name. Now `options_from` real querysets; approvers resolve empty until the crew module lands. |
| **Adding a company** | Needs the `create` permission **and** system scope. |

## Writes (Company module)

| Thing | Decision |
|---|---|
| **Two screens removed** | Station Address Mapping and Branch Department Mapping. Both edited data that already has a home on Branch — the station's number, address and coordinates (`design/02-company.md` 3.4), and the `departments` multiselect. Their Page rows are deactivated rather than deleted, so the permission history survives. |
| **Approval** | Everything saves approved and active. `want_approval` / `approval_status` stay on the models, to be wired per screen once those screens are named. |
| **Delete** | Permanent when nothing uses the row; otherwise the row is deactivated and the modal says where it is still linked, with counts. |
| **Many-to-many counts as a use** | `ProtectedError` only covers PROTECT foreign keys. A department sitting in a branch's Departments list raises nothing — Django would drop the join rows and the department would vanish from that branch. `writes.references()` asks before deleting, so that silent case is reported too. |
| **Validation** | Every error at once, each labelled as the drawer labels the field. Model fields carry their business names (`"Branch Code"`, not `short_code`) and the unique constraints carry sentences, so no message shows a column name. |
| **Permission on the write** | Every save and delete re-checks `has_permission` server-side and confirms the row is inside `scoped_to`. A hidden button is a courtesy; this is the control. |
| **The company is never posted** | For a company user the drawer has no company field, and any posted value is replaced before validation. The field stays in the form — dropping it would trade a sentence in the drawer for an IntegrityError, because "unique per company" can only be checked while the company is part of what is validated. |
| **Drawer changes** | `{% csrf_token %}`, a hidden `pk`, `data-scr-save` on the two Save buttons, `data-field` on the Active switch, and option values carrying row ids. Matching a foreign key by display name is unsafe now that two companies can each have an "Operations". |
| **One delete handler** | The wireframe's `byky-screen.js` block removed the row and persisted nothing ("a real delete would call an API from here instead"). Removed, so it cannot race the real one. |
| **The kebab detaches** | `byky-screen.js` moves an open row menu into a body-level layer, so a Delete click happens outside its row and `closest('.scr-row')` finds nothing. The row is remembered when the menu opens instead. This is why the wireframe's own delete worked: its handler ran after the menu had been put back. |
| **New file, not edits** | Saving, deleting, the modals and the toast live in `static/js/byky-crud.js`, so the ported `byky-drawer.js` stays diffable against the wireframe. |
| **Modal typography** | `.scr-modal` now names its own font family, size, ink and margin resets. It had only ever been used inside a `.scr` screen, which supplied those; included after that wrapper closes, it fell back to the page font and looked like a different product. |
| **Design** | `DESIGN.md` extends the `.scr-*` guideline with the confirm, message and toast patterns; the modals compose from the `.scr-modal` classes already in the CSS. |

## Crew module

| Page code | Screen | Reference | Status | Date | Differences |
|---|---|---|---|---|---|
| `crew.employee` | Employee | `hrms_employee_personal_data.html` | done | 2026-09-19 | **The document-expiry card is real.** The wireframe hashed the employee code into a bucket (its own docstring says so) so its filters had something to do; ours reads the expiry dates. The filter mechanism is unchanged. The Photo field had an empty `id` and bound to nothing — it has one now. Home Country Address moved to the Address screen: with one address table, holding it in two places is how they drift. |
| `crew.designation` | Designation | `hrms_employee_designation_master.html` | done | 2026-09-19 | Rank order reads the column instead of the hardcoded 1. Attendance method added — it is what decides how this job's staff are marked present. |
| `crew.employee_address` | Employee Address | `hrms_employee_temporary_address.html` | done | 2026-09-19 | The grid was a hardcoded empty row; it loops real rows now. Gained a Type column: one table with a type replaces the temporary-address screen *and* the two address columns on Employee. |
| `crew.block_unblock` | Block / Unblock | `hrms_employee_block_unblock.html` | done | 2026-09-19 | Every part was inert: the status badge said "Active" for everyone, and the log was a permanent empty state. All three are real, and blocking now **ends that person's open sessions** — required by `design/03-login.md` §4 and previously called by nothing. |
| `crew.attendance` | Attendance | **none** | done | 2026-09-19 | The wireframe has no attendance screen at all. Composed from existing `.scr-*` parts as a read-only monitor; the pattern is written up in `DESIGN.md` §5. |

**Schema, on the team lead's review:** Employee is four tables — `employee`,
`employee_detail` (documents and bank), `employee_designation` (postings
history), `employee_address` (typed). One drawer still writes them in one
transaction. Nationality is plain text, not a master, until someone needs to
report on it.

**Attendance is the module's one UUID table** (`00-findings.md` §9) and carries
`business_date` (§8) — it is written by devices in the field.

**Not ported, no models:** Grade Master, Duty Roster, Incentive, Target and their
branch mappings, and the HRMS privilege matrix.

**One scoping bug, found in review:** the address screen's State and Country
lists were scoped to where the company has branches. A person can live in an
emirate their employer has no branch in, so that address was impossible to
record. Those two lists are geography, not company data, and are unscoped --
the same reasoning already applied to Location on the Branch form.

**Two foreign keys wired at last:** `core.User.employee` and
`company.BranchApprovalAuthority`, both deferred until this module existed.

## Portal module — roles, users and privileges

| Page code | Screen | Reference | Status | Date | Differences |
|---|---|---|---|---|---|
| `system.role` | Roles | **none** | done | 2026-09-18 | The wireframe had no Roles screen — its privilege matrix worked against a hardcoded list of eight names. Built as a Tier A list + drawer from the same parts. Its Screen 13.2 specified a Role Code and an Authority Rank 1–10; our `Role` has neither and `rbac_schema.pdf` defines neither, so the model's shape was built rather than two columns invented. |
| `system.user` | Users | **none** | done | 2026-09-18 | New. `scope` is deliberately not on the form — cross-company access stays `manage.py set_user_scope`, not a dropdown anyone with Users/update can reach. Blocking is not here either: that is the person, on the crew screen, and an account and an employment stop for different reasons. Reset password is a one-field modal (`password_modal.html`) and closes every session the account holds. |
| `company.privileges`, `crew.privileges` | Privileges | `byky/partials/privilege_matrix.html` | done | 2026-09-18 | See below. |

**Five changes to the ported privilege matrix**, each because the wireframe had
no data behind it:

1. **Keyed by primary key, not role name.** Names are editable and two companies
   may each have an "Operations".
2. **Checkboxes carry `data-page` and `data-action`.** They had no `name`,
   `value` or `id` and were aligned to the column list by position, which cannot
   be posted reliably.
3. **A cell exists only where the screen offers that action.** `Page.actions`
   varies — Attendance is read and print — and `has_permission()` already
   refuses an action a page does not offer. A box that can never mean anything
   reads like a grant that was made.
4. **Save, Add and Remove reach the server.** All three were inert.
5. **No `<template>` clones.** A wireframe `<template>` is consumed on first
   use, so re-adding a removed role silently did nothing. Every write reloads
   instead — the Full Access badge, the Add Role menu and the matrices all
   derive from the same rows, and re-deriving them in JS is how they drift.

**One addition the wireframe has no concept of:** a save that would remove the
acting user's own access to the privilege screen they are standing on is
refused. See `design/rbac.md` §6.

**Pages moved to a registry.** `apps/portal/page_registry.py` lists every module
and screen; `apps/portal/page_sync.py` applies it, called by `manage.py
sync_pages` and by migration `portal.0011`. Retired screens stay listed and are
deactivated, never deleted — their `RolePermission` rows are history.

**One drawer field kind added:** `password`, which renders `type="password"`
with `autocomplete="new-password"`. Every other kind renders its value; this one
must not.

**One latent bug found on the way:** `apps/company/writes.py::_takes_user` read
only `form_class.__init__`'s signature, so a form overriding `__init__` as
`(self, *args, **kwargs)` — `BranchForm`, `DesignationForm`, `EmployeeForm` —
was built **without the signed-in user**: no company filled in, unscoped
dropdowns, and a "Company is required" error about a field hidden from the
person reading it. It now searches the whole MRO.

### Dropdowns that had nothing behind them

`theme.drawers.resolve` treats an `options_from` key the context does not carry
as an empty list, on purpose — a 500 on an unrelated screen is worse than an
empty select. The cost is that the mistake is invisible, and it happened twice:

- **The crew screens never supplied `companies_list`.** A system user's Company
  dropdown on Add Employee and Add Designation was empty, and the field is
  required, so nobody could complete the save. A company user never sees the
  field at all, which is why it went unnoticed.
- **The Branch drawer's three approval pickers read `employees`**, which the
  view hardcoded to `[]` with a note saying employees arrive with the crew
  module. They arrived; the note stayed. Worse, `BranchForm` does not own those
  fields, so the drawer collected three lists and dropped them — they looked
  recorded and were not. They now write `BranchApprovalAuthority` through a new
  `EntitySaveView.after_save` hook, inside the same transaction, scoped to the
  branch's own company.

`apps/portal/tests/test_drawer_options.py` renders every screen as a **system
user** — the only scope that sees every field — and asserts each `options_from`
names a key the view supplies and resolves to something. Its fixture carries one
row of every kind on purpose, so an empty list can only mean broken wiring.

## Devices module (added 2026-09-18)

| Thing | Decision |
|---|---|
| **Mapping keeps history, and owns the station outright** | `DeviceMapping` carries `from_date`/`to_date`, one open row per device, and a re-map closes the old row rather than editing it — the legacy's own behaviour. An earlier pass also kept `Device.branch` as a denormalised current pointer; it was **dropped** (migration `0005`) because two writers for one fact can drift, and at 84 stations the join it saved is not worth that. `Device.current_branch` reads the open row. |
| **A lost check constraint, named** | `device_operator_needs_branch` could not survive the move — a check constraint cannot read another table. "An approved operator device must have a station" now belongs to the approval service, next to the multi-device rule, which was never expressible in SQL either. This is a real trade: a database guarantee became a code guarantee. |
| **`device_registration_id`** | Its own Postgres sequence starting at 1000, not the primary key. The number is shown on the tablet's waiting screen and read down a phone line to the admin, so it must not move if the key ever does. |
| **Multi-device rule** | `Branch.is_multi_device` cannot be a check constraint either, so it belongs to the mapping service, with `select_for_update` on the branch. Not enforced in this pass: there are no writes yet. |
| **No device secret** | The registration id identifies; the login token authorises (`03-login.md` decision 17). |
| **Approval has no `create` action** | A device joins the queue by registering itself. An admin who could create one by hand could invent a station's identity. No `delete` either — a device is retired. |
| **No To Date column on the mapping grid** | Each row is the device's **open** mapping, so `to_date` is null by definition — the legacy grid showed only current mappings too. The dates a move produces are in the device's station history, and the row kebab links to it. |
| **Lat/long kept, moved** | The legacy's `SfaDeviceMapping.Latitude/Longitude` are carried over as `Device.last_latitude` / `last_longitude` / `last_location_at`. On the mapping row they recorded where an operator stood at login and never changed again; on the device they are a last known position, refreshed by the calls the app already makes. Not a trail — that is tracking's job. |
| **Writes** | None. Approve, Reject, Block, Re-map and Close mapping render and are permission-gated, but post nowhere; they land with the CRUD pass, as they did for Company and Crew. |

## Date fields and static assets (19 Sep 2026)

Found while adding the Device Mapping drawer: its From Date rendered as a plain
text box, and so did every other date in the project.

| Thing | What was wrong, and what now stops it |
|---|---|
| **Date pickers never worked** | `drawer.html` renders kind `date` as a text input carrying `.byky-date`, waiting for JavaScript to upgrade it. Neither **flatpickr** nor the wireframe's `byky-hrms.js` (the file that binds it) was copied in the asset port, so all five Employee dates, the Attendance filters and Device Mapping's From Date were boxes asking the user to guess a format. flatpickr (148 KB) is now in `static/vendor/libs/flatpickr/`, and the binding lives in a new **`static/js/byky-datepicker.js`** — generic, because a date field can appear in any module's drawer, not just HRMS. `byky-hrms.js` keeps only the crew Block/Unblock accent switch. |
| **Per-page, not global** | `layout/partials/scripts.html` says per-page libraries belong in each template's `vendor_js`. Kept that way; the omission is caught by a test instead of prevented by loading 148 KB on every page. |
| **Drawers now name their model** | Each spec carries `"model": "app.Model"`, so a test can compare the drawer with the model behind it. A `DateField` rendered as anything but kind `date`/`datetime` fails the build, and the reverse too (`apps/portal/tests/test_date_fields.py`). This is the check that would have caught the port at source: the model said `DateField`, the drawer said text, and nothing compared them. |
| **A screen with a date must load the picker** | Same test renders the screen and asserts both files are there. Verified by breaking it on purpose: removing the script fails with the reason and the fix. |
| **Every `{% static %}` target must exist** | `apps/portal/tests/test_static_references.py` reads all 82 references out of the templates and checks them on disk. In development a missing file is a silent 404; with WhiteNoise's manifest storage in production it is a 500 on that page, and the test suite could not see it because `conftest.py` swaps in plain storage. |
| **Customizer references removed** | It found three more: `template-customizer.js`, `pickr.js`, `pickr-themes.css`. All Vuexy customizer assets, dormant behind `has_customizer` (pinned `False` — light theme only, no theme switcher) and never copied. Removed rather than left pointing at nothing. |

## App releases, device settings and bill continuity (19 Sep 2026)

Models, migrations and tests only; the screens come later. Every field was kept
or dropped on what the **legacy code** does with it — read for a decision,
printed, sent to the tablet — never on whether QA happens to hold values.

| Thing | Decision |
|---|---|
| **`app_session.session_key` dropped** | Written at web sign-in and indexed, never read. Force logout works the other way round: the Django session stores the `app_session` id and the middleware checks the row on every request. |
| **No `is_default` on a release** | The legacy's `IsDefault` was written on every upload and read by nothing — its fallback ordered by `APKVersionID DESC` — and all 12 live rows carried it at once. "All branches" is a mapping row instead, which can be switched off. |
| **Mandatory / Any time lives on the release** | One dropdown (`update_type`), one answer to "is this build mandatory?". Exposed to the app as the boolean `is_mandatory`. |
| **One index holds both mapping rules** | `(company, channel, branch) WHERE is_active` with `NULLS NOT DISTINCT`: one active mapping per branch per app, *and* one All row per app. Verified in Postgres that without `NULLS NOT DISTINCT` two All rows coexist. |
| **A composite foreign key, hand-written** | A mapping copies its release's `company` and `channel` so the index can see them; `devices/0007` adds `(release_id, company_id, channel) → app_release` so Postgres refuses any mismatch. `makemigrations` will never regenerate it — keep it if migrations are ever squashed. |
| **`app_release` dropped and recreated, not altered** | 0 rows, no references, every column changed. |
| **Tax and discount type moved to `Company`** | The legacy stored them on every settings row but `Save_Company_TaxSEttings` overwrote them all from the company, so they were only ever one value. |
| **16 legacy settings columns not carried** | The van-sales group, `GPSTimeInterval`, `TransNoStartCharacter`, `MainDisplay1/2`, `BillCopy`, `BillPrintType`, `PreviewLogo`, `LogoName`, `IsImport`. Decisive evidence: the C# mapper that built the tablet's payload (`SFA.cs:828-918`) dropped the whole van-sales group. Details in `03-login.md` §8.7. |
| **`additional_header_1` restored** | Printed on every legacy receipt (`Header3`), missing from the wireframe. |
| **`share_on_whatsapp` added** | No legacy column. From the client's feedback doc (row 60) via the wireframe — the one settings field not backed by the legacy. |
| **Settings defaults** | What every legacy row holds: paper feed 4, landscape, upward rounding to 25 fils, one copy, five-minute test slots. |
| **Wireframe settings items not carried** | a "5 Fils" round-off step (the legacy has only 25 fils / 50 fils / 1 AED); a paper-feed list of 1–3 that leaves out the legacy default of 4; a "Discount Type" select whose options are actually the tax type's. |
| **No settings history table** | The legacy's trigger-written history let a reprint show the header of its bill date. The rental order will snapshot what it printed instead. |
| **Bill numbers are a counter, not a block** | The legacy never reserved a range; the device's number inside every receipt keeps devices apart. `design/00-findings.md` and `03-login.md` §2 said otherwise and are corrected. |
| **Bill format unchanged** | `[T]` + prefix + device registration id + six digits, no separator — so receipts look the same to cashiers and customers. Built in one place, `services.format_bill_number`, instead of on the tablet. |
| **Counter raised, never incremented** | `GREATEST(last, reported)`. The legacy's `+1` per upload double-counted retries and needed an `IsLogin` flag to stop late uploads counting at all. |
| **Migration id sequence repaired** | Restoring `django_migrations` during the squash rollback left its id sequence at 1, so the next migration collided with row 1. Reset to `MAX(id)`. |

## First API: app update check (19 Sep 2026)

`POST /api/v1/{app}/app/update-check` — `design/03-login.md` §9A,
handout `design/registration/update-check-api.md`.

| Thing | Decision |
|---|---|
| **Shared API plumbing in `core/api.py`** | Envelope, `{app}` URL converter (`operator`/`manager`/`employee` only), `PublicAPIView` for pre-login calls, `request_parts` for the `{credentials, request_data}` body, and the DRF exception handler. Registration and login reuse it. Unknown `/api/` URLs answer a 404 in the envelope, not Django's HTML page. |
| **`credentials` never validated** | Accepted whole, even if it is not an object. An endpoint reads a key only when a feature needs it. |
| **`request_data` type-checked only** | A wrong type is `400 invalid_request` naming the field; a missing version is `up_to_date` + a warning log (§9A.7 row 12). |
| **Fails open all the way** | `update_decision_for_installation` now also catches errors in the device lookup, not only in the release lookup (§9A.7 row 13). |
| **SimpleJWT removed from DRF's default authentication** | Its module imports `django.contrib.auth.models`, which cannot load because that app is deliberately not installed — the first DRF view would not import. No API takes a token yet; how tokens are checked is settled with the login API. Default permission stays `IsAuthenticated`. |
| **`UNAUTHENTICATED_USER = None`** | DRF's default `AnonymousUser` lives in `django.contrib.auth.models` too. |
| **Throttle trusts the connection address** | `NUM_PROXIES` from `DJANGO_NUM_PROXIES`, default 0, so `X-Forwarded-For` cannot choose the bucket. **Set it in production** behind a load balancer, or every tablet shares one bucket. Uses the default in-memory cache — per process; move to Redis when it arrives. |

## Branch Working Time rewired, and "Any time" waits for it (19 Sep 2026)

| Thing | Decision |
|---|---|
| **The screen now loads and saves** | Picking a branch fetches `branch-working-time/schedule/<id>/`; Save posts the week; nothing reloads. `?branch=<id>` keeps the branch across a refresh. `static/js/byky-working-time.js`. |
| **Day names** | The template looped `WeekDay.choices` tuples, printing `(0, 'Sunday')`. Now labels. |
| **Branch options carry the id** | They carried only the name, while save expected the id. Options come from the user's active branches. |
| **Shift rules** | `apps/company/services.py::validate_schedule`, mirrored in the JS: both times or neither; end after start (no midnight crossing — 0 of 560 legacy rows cross it; all day is 00:00–23:59); shifts added in order; each starts after the previous ends. Bad values are a 400, never a 500. |
| **Loading veil** | Rows blur and a spinner sits over them — never the header — for at least 1.5 s on load, save and assign (client's request). |
| **Assign to multiple branches** | New modal: search, All branches / All matching, sticky-header list with a Set / Not set badge, current branch excluded. `branch-working-time/assign/` is all or nothing: one foreign id writes nothing. |
| **`window.BykyCrud`** | `byky-crud.js` now exports `post`, `toast`, `showMessages`, `open`, `close` so the screen reuses the CSRF post and the error modal instead of copying them. |
| **Any time updates wait for working hours** | Client requirement. `branch_open_state` (company timezone) gates only Any time releases; closed → `outside_working_hours`, none set / no branch → `working_time_not_set`, both 200. Mandatory never held. Existing release-routing tests now give their stations an all-day schedule or use mandatory releases, so they still test routing, not hours. |

## Device registration API and Device Approval actions (19 Sep 2026)

`POST /api/v1/{app}/device/registration` and the five approval actions. Rules
in `design/03-login.md` §9B.7a.

| Thing | Decision |
|---|---|
| **Device Approval now writes** | Approve (with an optional "Replaces an existing device" that prefills name and station), Reconnect (with "Register as new"), Reject, Block, Unblock — from the row menus and the detail page. Modals in `devices/partials/device_action_modals.html`, logic in `static/js/byky-device-approval.js`, rules in `services.py`. |
| **Two rate limits** | `registration_ip` 30/min and `registration_installation` 12/min. |
| **`byky-crud.js` exports `toastAfterReload`** | So a screen outside the drawer flow can report after its reload. |
| **Detail page gained actions** | Only the ones valid for the device's status, gated by permission. |
| **No push on approval** | Recorded in the handout; the tablet polls. |

## Device Mapping writes (19 Sep 2026)

Add, Re-map and Close mapping were rendered but inert: the drawer had no save
URL, Re-map opened the Add drawer in an edit mode with no record to prefill,
and its Device list held only *unmapped* devices -- so the device being moved
was never in it. Close mapping had no handler.

| Thing | Decision |
|---|---|
| **Add** | The drawer now saves (`/devices/mapping/save/`). Its list: approved **and blocked** devices with no station — a blocked operator device must be mapped before Unblock. |
| **Re-map** | Its own modal (new station + From Date), not the drawer. Closes the open row on the new From Date and opens a new one (§8.6, half-open). From Date between the current mapping's start and today; not the same station; station rule as Approve. |
| **Close mapping** | Ends the mapping today; the row stays in the history. |
| **Refused while in use** | Server-side too, not only the disabled button: any open session on the device. |
| **Permissions** | Add = create, Re-map = update, Close = delete — as the buttons already showed. |

## Device Approval changes (19 Sep 2026, client decisions)

| Thing | Decision |
|---|---|
| **Station optional at approval** | Device Mapping owns stations. An operator tablet with none cannot log in until mapped. Unblock no longer demands one. |
| **No "Replaces an existing device" picker** | Removed from the Approve modal: a hand-picked list of every device was not workable. The service keeps `replaces_pk`. |
| **Registration-number lookup** | The Pending search takes digits only; an exact match shows a card with the request and its actions (Enter = Approve), or says it is already approved / blocked / not found. |

## Device Mapping dates are stamped, not typed (19 Sep 2026)

| Thing | Decision |
|---|---|
| **No From Date input** | The first port replaced the wireframe's mirror fields with a From Date input; the legacy never had one — it stamped now on save and on re-map. Removed from the Add drawer and the Re-map modal, with the date picker the page loaded only for it. |
| **Date and time** | `devices/0010`: `from_date` / `to_date` became timestamps. Existing rows became midnight Dubai time (hand-written cast; a plain AlterField would have made them midnight UTC). |
| **Grid and history** | "Mapped Since" and the station history show date and time. |

## App Releases and App Mapping (19 Sep 2026)

Two screens in the Devices sidebar feeding the update check
(`design/registration/update-check-api.md`). The legacy had the same two
(`UploadAPK.aspx`, `ApkMapping.aspx`); §9A.6 had planned one.

| Thing | Decision |
|---|---|
| **App Releases** | List + drawer: App, Version name, Version code, **Update type (Mandatory / Any time)**, Download link, What's new; company shown read-only. |
| **Version code kept** | The tablet sends the code it has *installed*; the server compares it with the *release's* code (handout §6.1). Must be higher than every earlier release of that app, withdrawn ones included (§7.1). |
| **No https rule** | Client decision: any well-formed link. |
| **Edit** | Only update type, link and note. App, name and code lock — tablets compare against them. Changing the update type of a build branches are on asks first. |
| **Withdraw / Restore, no Delete** | The confirm says where the branches go (All-branches build, or no update) — the same rule `release_for` applies. |
| **App Mapping** | One tab per app, plus History. An "All branches (default)" card; then every active branch with the build it is offered now, its source (own / All / none), update type and whether working time is set (an Any time build is held back where it is not). |
| **Map Release** | Pick a build; All branches or picked branches; the server previews who moves and returns warnings — older build than All, Mandatory, Any time at branches with no working time. Save stays disabled until each is ticked, and the server refuses an unconfirmed save that has warnings. |
| **Nothing deleted** | Re-mapping switches the old row off; Remove switches a row off. Both show in History. |
| **Drawer** | Shared drawer gained `hide_active`, `note_from`, and `lock_on_edit` on selects and numbers. |

## Device Settings (20 Sep 2026)

The screen for section 8.7, and the model checked against the client's live
rows (`Byky Live Data.xls`, `SfaDeviceSetting`, 95 rows). Nothing imported.

| Thing | Decision |
|---|---|
| **Per-station row kept** | The live data justifies it: Header 1 is the station (74 of 95 match the branch name), Header 2 the emirate, Additional Header 1 the **trade licence the station operates under** (Dubai/Ajman `RENTAL & TRADING LLC`, Sharjah `L.L.C - S.P`, RAK/Abu Dhabi/Al Ain `OPC`), the logo a bitmap per station, the prefix unique. Not one company-wide template. |
| **Drift is a screen problem, not a schema one** | The same legal name appears in four spellings and one footer in two, after years of editing 95 rows by hand (37 edited on one day in Nov 2025). So the drawer has **Copy from another station**, and the row menu **Copy to other stations** for the shared wording and printer traits. Station, code, prefix, Header 1/2 and the logo are never copied. |
| **Logo stored as bytes in the row** | `ImageField` → `BinaryField` + `logo_name` (migration `devices/0011`). The live logos are **1-bit BMPs 832 dots wide** — the 80 mm print head's own width — averaging 25 KB, 2 MB for the network. Stored exactly as uploaded, never re-encoded: these bytes are what the printer consumes. No media volume to back up separately, and no row pointing at a missing file. |
| **The logo travels with the settings** | Base64 in the login payload, as the legacy did, because a station prints all day without a network. Left out when the tablet's copy is current (`logo_changed_on` vs its last sync), which is the legacy's own rule. |
| **Deferred by default** | `DeviceSettingsQuerySet` defers the column; `with_logo()` asks for it. A 95-station grid would otherwise carry 2 MB of bitmap it never shows. |
| **Odd bitmaps warn, never block** | Not a BMP, not 1-bit, wider than 832 dots — said, then saved. The client's own data has a 32-bit 100×100 BMP in it. |
| **Paper feed default 4 → 2** | 60 of the 95 live rows. The old comment described QA, not live. |
| **Deactivate, not delete** | `bill_continuity` copied the prefix and receipts printed under it are still out there. |
| **Print Type, Settings Code, Additional Header 2 kept** | Though the legacy only ever offered Landscape (`Enumeration.cs:334`, Portrait commented out) and Header4 is empty in 94 of 95 rows — the client chose to keep all three. |

## Device Settings: the live data imported (20 Sep 2026)

95 stations, logos included. `tools/extract_device_settings.py` reads the
client's workbook into `data/legacy/sfa_device_setting.json` plus
`data/legacy/device_logos/*.bmp`; `manage.py import_legacy_device_settings`
loads them (`--dry-run` first).

| Thing | Decision |
|---|---|
| **The workbook is the source, not QA** | QA holds 82 of the 95 rows — thirteen stations are missing — and 34 of the rows it does hold carry an older logo. Checked both before choosing. CLAUDE.md's "QA mirrors production" does not hold for this table. |
| **The workbook's logos are whole** | Worth checking, since Excel truncates a cell at 32,767 characters and the legacy stored each logo as base64 in the row. Every live value is 3,712–25,600 characters, and all 95 decode to valid bitmaps: 94 are 1-bit and **832 dots wide** (the 80 mm print head's width), one is 240. `PreviewLogo` *is* truncated, and is the same picture again, so §8.7 drops it. |
| **Bytes in, bytes out** | The bitmap is decoded once by the extractor and stored unchanged. `settings_payload` base64s it again for the tablet, and the result is **character-for-character the string the legacy sent** — verified against the workbook for `creek1` (25,044 characters, 18,782 bytes). |
| **Legacy ids and dates kept** | `DeviceSettingID` is the primary key and `BranchID` already is `Branch.pk`. `LogoChangedOn` is imported as it stands: a tablet re-downloads its logo only when that moment passes its last sync, so stamping them all "now" would send every station to fetch a picture it already holds. |
| **Codes become words** | `PrintType 1` → Landscape (its enum has no other member), `RoundOff 0/1/2` → Nearest/Upward/Downward, `RoundOffLimit 0/1/2` → the amounts 0.25/0.50/1.00 (`SFA.cs:910-914`), a NULL test slot → 5 minutes (the tablet proc's own ISNULL). |
| **Share on WhatsApp off everywhere** | It has no legacy column; it came from the client's feedback on the new screens. |
| **Nothing conflicted** | All 95 rows pass our constraints as they stand: codes and prefixes unique, one active row per station, every length and range inside its limit. |

## Receipt numbering written up (20 Sep 2026)

`design/bill-continuity/bill-continuity.md` is now the design for how a rental
gets its printed number. `03-login.md` §8.8 stays as the table's summary and
points at it.

| Thing | Where it came from |
|---|---|
| **Six-digit padding** | The app did the padding, not the server — which only sliced the last six characters back out (`Save_Order_Booking.sql:234`). Confirmed against real numbers: `DUBPP60182000335`, `RMAA10132000842`, `AJSAP19011103`, `RAKSP9010563`. Lengths differ only because a prefix is 4–5 characters and a device id 1–5 digits. |
| **Prefix fallback** | `CoreBranch.ShortCode`, read at `Service_Operator_Login.sql:247-255` and used at 750. Our `Branch.short_code`, which `services.bill_prefix_for` already uses — no new field needed. |
| **The device id is part of the prefix the server sends** | Login concatenates it (`:746-755`) and `DMS.cs:319` upper-cases the result. |
| **Raise-forward, not `+1`** | The legacy counted upload events (`Save_Order_Booking.sql:426`), so a retry counted twice and `IsLogin` was bolted on to stop late uploads counting. Reporting the printed number makes both harmless and the flag unnecessary. |
| **Queues carry their operator** | The upload payload already did: `@UserID`, `@CreatedBy`, `@DeviceID`, `@BranchID`, `@OrderDate`, `@DeviceTime`. In all 164,019 live orders `UserID` = `CreatedBy`, and nothing recorded who *sent* it. |
| **Any operator on the device may clear a queue** | Client decision, 20 Sep 2026. The creator is kept; the uploading session is recorded from the token. Delayed uploads are real but rare: 142,416 within 5 minutes, 1,108 later the same day, 15 beyond 12 hours. |
| **The upload checks its session is open** | An indexed lookup by session id, since a 30-minute access token outlives a force logout (§6.2). |

`BillContinuity` needs no change for any of this — checked field by field against
the design. What RMS adds is listed in that file's §5: the raise-forward
service, the upload checks, the rental's own columns (including
`uploaded_by_session`), and login's reply.

## Multi-company (20 Sep 2026)

The deployment is one server with several companies — BYKY and others like it.
Registration had been built around `deployment_company_id()` ("one company per
deployment"), which answers only when exactly one active company exists, so a
second company would have stopped registration dead with `503`. Corrected:

| Thing | Decision |
|---|---|
| **The app names its company** | `company_id` in `request_data` (or in the `credentials` block), matched on `company.short_code`. The client's licence server issues the value; `short_code` is text, so a numeric licence id matches as readily as `BYKY`. |
| **Taken as sent, not verified** | Client decision. Approval is the control: a tablet naming the wrong company waits in that company's pending list to be rejected, and until approval has no station, no login and no data. A code matching nothing is `400 unknown_company`, and nothing is written. |
| **A registered tablet is never re-homed** | Its company was settled when it was approved; a later call naming another one is answered from its own row. |
| **The update check reads it too**, but only for a tablet the server has never seen — otherwise a new tablet on an old build could never learn about a mandatory release. A known tablet's row wins. |
| **A system user sees every company** | `_companies(user)` replaces the old single-company helper. Device Settings, App Releases and App Mapping show all companies a user manages, with a Company column and filter that appear only when there is more than one. |
| **A release names its company** | The drawer gains a Company field for system users only (`system_only`, dropped for company users by `theme.drawers.for_user`). A posted company is ignored for a company user — their own is used. |
| **Mapping is per company and per app** | The All-branches default and the one-active-row-per-branch index are both scoped that way, so each app tab holds one block per company, and the Map Release picker offers only that company's builds and branches. |

## Imports leave the id counter behind (20 Sep 2026)

Every import here writes the legacy's own ids, on purpose, so `BranchID` and the
rest still point where they did. Postgres's counter for a serial column only
moves when *it* supplies the id, so after an import the counter sat at 1 while
the table held id 97 — and the next branch, location, state or settings row
added from a screen failed with "duplicate key". Invisible until someone adds a
station, which is the worst moment to find it.

`legacy_import.write()` now calls `reset_sequence(model)`, which `setval`s the
counter past the highest id in the table. It runs for every import that uses
`write`, so device settings got it for free. Regression tests in both
`apps/company/tests/test_legacy_import.py` and
`apps/devices/tests/test_device_settings_import.py` add a row by hand after
importing. The dev database was repaired in place.

## Branch and settings codes are unique per company, not globally (20 Sep 2026)

Flagged when multi-company was added and fixed now: `Branch.short_code`,
`DeviceSettings.settings_code` and `DeviceSettings.order_no_prefix` were all
still globally unique, left over from before a second company existed.

| Thing | Decision |
|---|---|
| **Branch** | Needed no new field -- it already has `company` directly. `short_code` lost its bare `unique=True`; a `UniqueConstraint(company, short_code)` replaces it (`company/0007`). |
| **DeviceSettings** | Had no `company` of its own, only `branch`. Gained one -- a copy of `branch.company`, set in `save()`, never by a caller -- the same pattern `AppReleaseMapping.company` already uses (`devices/0007`) and for the same reason: a constraint cannot reach across to another table. A composite foreign key `(branch_id, company_id) -> branch (id, company_id)` (`devices/0012`) makes Postgres refuse a row whose copy ever disagrees -- checked live: forcing a mismatch by SQL is rejected. |
| **Backfill** | All 95 existing settings rows backfilled from their own branch's company in the same migration; no manual step. |
| **Receipts stay unique regardless** | A code repeating across companies cannot collide on a printed receipt -- the device's own registration id sits inside every number (§8.8), which is company-agnostic. |
| **The service layer needed its own fix, not just the constraint** | A system user's `settings_qs` spans every company, so `_check_settings`'s clash queries had to be scoped by the branch's (or the edited row's) `company_id` explicitly -- the database constraint is the last line of defence, not the only one. Proven with a system user saving the same code into two companies through `save_settings` itself. |

666 tests pass (8 new: 3 for Branch, 5 for DeviceSettings), no migration drift.

## Duty Roster: employee-first flow, scoped to Oct 2 (20 Sep 2026)

New module, not a legacy port -- `design/duty roster/business-logic.md`
already established there is no legacy roster, duty, shift or leave table to
carry forward. Built from the client's own mockup plus their verbal
instructions the same day, which settled several things the analysis file had
left open; see `design/duty roster/duty-roster.md` for the full account.

| Thing | Decision |
|---|---|
| **Employees are not branch-mapped for staffing** | `Employee.branch` stays (home/base fact, used elsewhere) but `DutyRoster` never reads it -- every day picks its own branch. |
| **Cashier/Labour only** | New `Designation.roster_category` field; every other designation never appears in the roster's pickers. |
| **Shift times load from `BranchWorkingTime`, not typed from nothing** | `services.branch_shift_defaults(branch, date)` -- Shift 1 always, Shift 2 only when that branch/weekday has one. Verified live against Creek Park 1's real 07:00-23:00 schedule. |
| **Timezone bug caught and fixed during testing** | Shift times were first combined using Django's global default timezone; fixed to use the branch's own company timezone (`core.timezones.zone_for`), the same convention `company.services.local_now` already uses. Caught by a test comparing a saved 08:00 shift against its stored UTC value. |
| **Overnight applies to either shift, not just Shift 2** | Both shift inputs are plain HH:MM with no date of their own, so "end before start" rolls onto the next day for either one -- matching the analysis file's own wording, which never restricts overnight to Shift 2. This makes an "end must be after start" input error unreachable by design, not a bug. |
| **Only employee-first is built** | Branch-first and Excel upload share the same `save_week`/`DutyRoster` engine but are not wired to a screen yet. |
| **Leave and Attendance are seams, not features** | `today_assignment` carries a documented TODO for where an approved Leave Request will override its answer; `absent_today` raises `NotImplementedError` with its intended shape. Neither table exists. |
| **No seed data** | Waiting on the client to name Creek Park 1's real cashiers/labour; nothing here is fabricated. |

28 new tests (`apps/crew/tests/test_duty_roster.py`), 697 in the whole suite,
no migration drift.

