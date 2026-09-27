"""The seven fleet masters -- Brand, Category, Vehicle Type, UOM, Asset Type,
Asset, Vehicle -- held to one contract, table-driven.

Every master is a list screen with a drawer, written through the generic
save/delete views (apps.company.writes). What each one must do:

- save creates for the signed-in user's company (never the payload's),
  approved and active; saving again with its pk updates, never duplicates;
  delete removes an unused row;
- errors name the drawer's labels, never a column name; a duplicate code is
  refused in words; a code is unique per company, not across companies;
- create, update and delete each need their own permission;
- another company's rows are out of reach (404), and so are its rows as
  references -- a vehicle type cannot point at their category;
- a company user's screen never shows another company's data -- rows, drawer
  options, anything -- and is never asked which company; a system user sees
  every company and must choose one.

Every row of the second company carries "Theirs" in its name or code, so
"the page never says Theirs" is the whole scoping check.
"""

import json
from dataclasses import dataclass, field

import pytest
from django.db import IntegrityError, transaction

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.crew.models import Designation, Employee
from apps.fleet.models import UOM, Asset, AssetType, Brand, Category, Vehicle, VehicleType
from apps.portal.models import Role, RolePermission
from apps.portal.services import grant_all
from core.enums import ApprovalStatus, Channel, UserScope
from core.models import User

PASSWORD = "Byky#2026"


# -- The masters -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Master:
    name: str                 # as the save message says it: "Vehicle Type saved."
    slug: str                 # /fleet/<slug>/...
    model: type
    make: callable            # (company, refs, tag) -> a saved row
    payload: callable         # (refs) -> a valid new row for our company
    text: str                 # a field an update changes
    required: tuple           # labels an empty save must name
    column: str               # a column name an error must never use
    drawer_field: str         # a field the drawer must offer
    code: str = ""            # the code unique per company, if any
    picks: tuple = field(default=())   # (form field, ref key): references that must be ours

    @property
    def page(self):
        return f"fleet.{self.slug.replace('-', '_')}"

    def __str__(self):
        return self.name


MASTERS = [
    Master(
        "Brand", "brand", Brand,
        lambda co, r, t: Brand.objects.create(company=co, brand_code=f"RB{t}", brand_name=f"{t} Row Brand"),
        lambda r: {"brand_code": "NEWB", "brand_name": "New Brand", "manufacturer": "Byky Motors",
                   "website_link": "https://byky.test"},
        "brand_name", ("Brand Code",), "brand_code", "brand_code", code="brand_code",
    ),
    Master(
        "Category", "category", Category,
        lambda co, r, t: Category.objects.create(company=co, category_code=f"RC{t}",
                                                 category_name=f"{t} Row Category"),
        lambda r: {"category_code": "NEWC", "category_name": "New Category", "description": "Main line"},
        "category_name", ("Category Code",), "category_code", "category_code", code="category_code",
    ),
    Master(
        "Vehicle Type", "vehicle-type", VehicleType,
        lambda co, r, t: VehicleType.objects.create(company=co, category=r["category"], brand=r["brand"],
                                                    vehicle_type_name=f"{t} Row Type"),
        lambda r: {"category": r["category"].pk, "brand": r["brand"].pk, "vehicle_type_name": "New Type",
                   "vehicle_type_code": "NEW", "tax_percentage": "5.00", "other_tax": "1.50"},
        "vehicle_type_name", ("Vehicle Type Name",), "vehicle_type_name", "vehicle_type_name",
        picks=(("category", "category"), ("brand", "brand")),
    ),
    Master(
        "UOM", "uom", UOM,
        lambda co, r, t: UOM.objects.create(company=co, uom_code=f"RU{t}", uom_name=f"{t} Row UOM"),
        lambda r: {"uom_code": "NEWU", "uom_name": "New UOM"},
        "uom_name", ("UOM Code",), "uom_code", "uom_code", code="uom_code",
    ),
    Master(
        "Asset Type", "asset-type", AssetType,
        lambda co, r, t: AssetType.objects.create(company=co, asset_type_name=f"{t} Row Asset Type"),
        lambda r: {"asset_type_name": "New Asset Type", "asset_type_code": "NAT"},
        "asset_type_name", ("Asset Type",), "asset_type_name", "asset_type_name",
    ),
    Master(
        # Made bare -- no brand, custodian or branch -- so the list's
        # optional-reference path is rendered too.
        "Asset", "asset", Asset,
        lambda co, r, t: Asset.objects.create(company=co, asset_code=f"{t}-AST", asset_type=r["asset_type"]),
        lambda r: {"asset_code": "NEW-AST", "asset_type": r["asset_type"].pk, "brand": r["brand"].pk,
                   "custodian": r["employee"].pk, "branch": r["branch"].pk, "serial_no": "SN-1",
                   "warranty_from_date": "2026-01-01", "warranty_to_date": "2028-01-01"},
        "serial_no", ("Asset Code", "Asset type"), "asset_code", "asset_code", code="asset_code",
        picks=(("asset_type", "asset_type"), ("brand", "brand"), ("custodian", "employee"), ("branch", "branch")),
    ),
    Master(
        "Vehicle", "vehicle", Vehicle,
        lambda co, r, t: Vehicle.objects.create(company=co, vehicle_code=f"{t}-VEH", vehicle_name=f"{t} Row Vehicle",
                                                vehicle_type=r["vehicle_type"], uom=r["uom"], rfid_epc=f"EPC-{t}"),
        lambda r: {"vehicle_code": "NEW-VEH", "vehicle_name": "New Vehicle", "vehicle_type": r["vehicle_type"].pk,
                   "uom": r["uom"].pk, "rfid_epc": "EPC-NEW", "is_available": False,
                   "current_branch": r["branch"].pk},
        "vehicle_name", ("Vehicle Code",), "vehicle_code", "vehicle_code", code="vehicle_code",
        picks=(("vehicle_type", "vehicle_type"), ("uom", "uom"), ("current_branch", "branch")),
    ),
]
WITH_CODES = [m for m in MASTERS if m.code]
PICKS = [pytest.param(m, form_field, ref, id=f"{m.slug}-{form_field}") for m in MASTERS for form_field, ref in m.picks]
# -- The world ----------------------------------------------------------------------------------


@pytest.fixture
def world(db):
    uae = Country.objects.create(short_code="AE", name="United Arab Emirates")
    abu_dhabi = State.objects.create(country=uae, short_code="AUH", name="Abu Dhabi")
    location = Location.objects.create(country=uae, state=abu_dhabi, short_code="CORN", name="Corniche")
    ours = Company.objects.create(short_code="BYKY", name="BYKY", country=uae, state=abu_dhabi,
                                  phone_number="+9710000000", email="ops@byky.test")
    theirs = Company.objects.create(short_code="OTHER", name="Other Co", country=uae, state=abu_dhabi,
                                    phone_number="+9711111111", email="ops@other.test")

    def refs(company, tag):
        category = Category.objects.create(company=company, category_code=f"C{tag}", category_name=f"{tag} Category")
        brand = Brand.objects.create(company=company, brand_code=f"B{tag}", brand_name=f"{tag} Brand")
        designation = Designation.objects.create(company=company, code=f"M{tag}", title="Mechanic", rank_order=1)
        return {
            "category": category, "brand": brand,
            "vehicle_type": VehicleType.objects.create(company=company, category=category, brand=brand,
                                                       vehicle_type_name=f"{tag} Type"),
            "uom": UOM.objects.create(company=company, uom_code=f"U{tag}", uom_name=f"{tag} UOM"),
            "asset_type": AssetType.objects.create(company=company, asset_type_name=f"{tag} Asset Type"),
            "employee": Employee.objects.create(company=company, employee_code=f"E{tag}", first_name=f"{tag} Anil",
                                                designation=designation),
            "branch": Branch.objects.create(company=company, location=location, short_code=f"S{tag}",
                                            name=f"{tag} Station", branch_type=BranchType.STATION),
        }

    return {"ours": ours, "theirs": theirs, "our": refs(ours, "Ours"), "their": refs(theirs, "Theirs")}


def sign_in(client, username, **fields):
    role = Role.objects.create(company=fields.get("company"), name=f"Role {username}")
    grant_all(role)
    User.objects.create_user(username, PASSWORD, display_name=username, role=role,
                             allowed_channels=[Channel.WEB], **fields)
    client.post("/login/", {"username": username, "password": PASSWORD})
    client.role = role
    return client


@pytest.fixture
def client_in(client, world):
    """A company administrator of "ours"."""
    return sign_in(client, "sara.k", company=world["ours"])


@pytest.fixture
def system_client(client, world):
    return sign_in(client, "platform.admin", scope=UserScope.SYSTEM)


def post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def save(client, master, payload):
    return post(client, f"/fleet/{master.slug}/save/", payload)


def delete(client, master, pk):
    return post(client, f"/fleet/{master.slug}/{pk}/delete/", {})


def error_fields(response):
    return {error["field"] for error in response.json()["errors"]}


# -- Writes -------------------------------------------------------------------------------------


@pytest.mark.parametrize("master", MASTERS, ids=str)
def test_save_creates_updates_and_delete_removes(client_in, world, master):
    ours = master.model.objects.filter(company=world["ours"])
    before = ours.count()

    created = save(client_in, master, {**master.payload(world["our"]), "company": world["theirs"].pk})
    body = created.json()
    assert created.status_code == 200, body
    assert body["message"] == f"{master.name} saved."
    row = master.model.objects.get(pk=body["pk"])
    assert row.company == world["ours"]                        # from the user, never the payload
    assert (row.is_active, row.approval_status) == (True, ApprovalStatus.APPROVED)

    updated = save(client_in, master, {**master.payload(world["our"]), "pk": row.pk, master.text: "Changed"})
    assert updated.status_code == 200, updated.json()
    row.refresh_from_db()
    assert getattr(row, master.text) == "Changed" and ours.count() == before + 1

    assert delete(client_in, master, row.pk).json()["ok"] is True
    assert ours.count() == before


@pytest.mark.parametrize("master", MASTERS, ids=str)
def test_errors_use_the_drawers_labels(client_in, master):
    response = save(client_in, master, {})
    assert response.status_code == 400
    fields = error_fields(response)
    assert set(master.required) <= fields and master.column not in fields


@pytest.mark.parametrize("master", WITH_CODES, ids=str)
def test_a_duplicate_code_is_refused_in_words(client_in, world, master):
    existing = master.make(world["ours"], world["our"], "Ours")
    response = save(client_in, master, {**master.payload(world["our"]),
                                        master.code: getattr(existing, master.code)})
    assert response.status_code == 400
    assert any("already exists" in e["message"] for e in response.json()["errors"])


@pytest.mark.parametrize("master", WITH_CODES, ids=str)
def test_a_code_is_unique_per_company_only(world, master):
    master.make(world["ours"], world["our"], "Same")
    master.make(world["theirs"], world["their"], "Same")            # another company: fine
    with pytest.raises(IntegrityError), transaction.atomic():
        master.make(world["ours"], world["our"], "Same")


@pytest.mark.parametrize("master", MASTERS, ids=str)
def test_each_write_needs_its_own_permission(client_in, world, master):
    row = master.make(world["ours"], world["our"], "Ours")
    before = master.model.objects.filter(company=world["ours"]).count()
    grants = RolePermission.objects.filter(role=client_in.role, page__code=master.page)
    attempts = {
        "create": lambda: save(client_in, master, master.payload(world["our"])),
        "update": lambda: save(client_in, master, {**master.payload(world["our"]), "pk": row.pk}),
        "delete": lambda: delete(client_in, master, row.pk),
    }
    for action, attempt in attempts.items():
        grants.update(**{f"can_{action}": False})
        response = attempt()
        assert response.status_code == 403 and response.json()["code"] == "forbidden", action
        grants.update(**{f"can_{action}": True})
    assert master.model.objects.filter(company=world["ours"]).count() == before        # nothing written


@pytest.mark.parametrize("master", MASTERS, ids=str)
def test_another_companys_row_is_out_of_reach(client_in, world, master):
    theirs = master.make(world["theirs"], world["their"], "Theirs")
    before = getattr(theirs, master.text)

    assert save(client_in, master, {**master.payload(world["our"]), "pk": theirs.pk,
                                    master.text: "Hijacked"}).status_code == 404
    assert delete(client_in, master, theirs.pk).status_code == 404
    theirs.refresh_from_db()
    assert getattr(theirs, master.text) == before


@pytest.mark.parametrize("master,form_field,ref", PICKS)
def test_another_companys_row_cannot_be_referenced(client_in, world, master, form_field, ref):
    before = master.model.objects.count()
    response = save(client_in, master, {**master.payload(world["our"]), form_field: world["their"][ref].pk})
    assert response.status_code == 400
    assert any("valid choice" in e["message"] for e in response.json()["errors"])
    assert master.model.objects.count() == before


def test_a_vehicle_rfid_tag_is_unique_but_may_be_left_blank(client_in, world):
    vehicle = next(m for m in MASTERS if m.model is Vehicle)
    base = vehicle.payload(world["our"])
    assert save(client_in, vehicle, base).status_code == 200
    duplicate = save(client_in, vehicle, {**base, "vehicle_code": "V-2"})           # same EPC
    assert duplicate.status_code == 400
    assert any("already exists" in e["message"] for e in duplicate.json()["errors"])
    for code in ("V-3", "V-4"):                                                     # two with no tag yet
        assert save(client_in, vehicle, {**base, "vehicle_code": code, "rfid_epc": ""}).status_code == 200
    assert Vehicle.objects.filter(rfid_epc="").count() == 2


# -- What each user sees -------------------------------------------------------------------------


@pytest.fixture
def both_rows(world):
    for master in MASTERS:
        master.make(world["ours"], world["our"], "Ours")
        master.make(world["theirs"], world["their"], "Theirs")


@pytest.mark.parametrize("master", MASTERS, ids=str)
def test_a_company_user_sees_only_their_company(client_in, both_rows, master):
    body = client_in.get(f"/fleet/{master.slug}/list/").content.decode()
    assert "Ours" in body
    assert "Theirs" not in body                              # no row, no drawer option, nothing
    assert f'data-field="{master.drawer_field}"' in body
    assert 'data-field="company"' not in body                # never asked which company


@pytest.mark.parametrize("master", MASTERS, ids=str)
def test_a_system_user_sees_every_company_and_must_choose_one(system_client, both_rows, master):
    body = system_client.get(f"/fleet/{master.slug}/list/").content.decode()
    assert "Ours" in body and "Theirs" in body
    assert 'data-field="company"' in body


def test_system_scope_does_not_bypass_permissions(system_client):
    RolePermission.objects.filter(role=system_client.role, page__code="fleet.brand").update(can_read=False)
    assert system_client.get("/fleet/brand/list/").status_code == 403


# -- Screens ------------------------------------------------------------------------------------


def test_the_sidebar_lists_exactly_the_screens_the_role_may_read(client_in):
    hidden = {"Brand", "UOM", "Vehicle"}
    RolePermission.objects.filter(role=client_in.role, page__code__in=["fleet.brand", "fleet.uom",
                                                                       "fleet.vehicle"]).update(can_read=False)
    body = client_in.get("/dashboard/").content.decode()
    for master in MASTERS:
        shown = f'class="menu-label">{master.name}</span>' in body
        assert shown == (master.name not in hidden), master.name
