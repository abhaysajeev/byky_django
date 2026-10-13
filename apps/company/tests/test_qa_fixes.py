"""QA report 13 Oct 2026, section A -- the Company screens' real bugs:
BUG-012 (state needs its country), BUG-014 (a new location / state stays
visible), BUG-017 (the branch list's Location filter), BUG-019/020
(coordinates in range), and two found while checking: a non-hotel branch
refused for its empty Hotel Commission, and Active switched off on Add
overwritten to active."""

from apps.company.drawers import STATE
from apps.company.models import Branch, Department, Location, State
from apps.company.tests import test_crud as crud

world, client_in, post, branch_payload = crud.world, crud.client_in, crud.post, crud.branch_payload


def context_of(client, url):
    response = client.get(url)
    assert response.status_code == 200
    return response.context


# -- BUG-012: a state is saved with its country ----------------------------------------

def test_the_state_drawer_asks_for_the_country():
    ids = [f["id"] for s in STATE["sections"] for f in s["fields"]]
    assert ids[0] == "country"


def test_a_state_is_added_and_edited_from_the_drawer(client_in, world):
    added = post(client_in, "/company/state/save/", {"country": world["country"].pk, "code": "DXB", "name": "Dubai"})
    assert added.status_code == 200, added.content
    dubai = State.objects.get(short_code="DXB")

    edited = post(client_in, "/company/state/save/", {"pk": dubai.pk, "country": world["country"].pk,
                                                      "code": "DXB", "name": "Dubai Emirate"})
    assert edited.status_code == 200, edited.content
    assert State.objects.get(pk=dubai.pk).name == "Dubai Emirate"


# -- BUG-014: what a company's people create stays in its lists ------------------------

def test_a_new_location_stays_listed_and_can_take_the_first_branch(client_in, world):
    response = post(client_in, "/company/location/save/", {
        "country": world["country"].pk, "state": world["state"].pk, "code": "MRN", "name": "Marina"})
    assert response.status_code == 200, response.content
    marina = Location.objects.get(short_code="MRN")
    assert marina.created_by.username == "sara.k"

    page = client_in.get("/company/location/list/").content.decode()
    assert "Marina" in page
    branch_screen = context_of(client_in, "/company/branch/list/")
    assert marina.pk in {loc["id"] for loc in branch_screen["locations_list"]}


def test_a_new_state_stays_listed(client_in, world):
    post(client_in, "/company/state/save/", {"country": world["country"].pk, "code": "SHJ", "name": "Sharjah"})

    assert "Sharjah" in client_in.get("/company/country-state/list/").content.decode()


def test_another_companys_new_location_is_not_listed(client_in, world):
    from core.models import User

    other_user = User.objects.create_user("other.u", crud.PASSWORD, company=world["other"])
    Location.objects.create(country=world["country"], state=world["state"], short_code="FAR", name="Faraway",
                            created_by=other_user)

    assert "Faraway" not in client_in.get("/company/location/list/").content.decode()


# -- BUG-017: the Location filter offers locations ---------------------------------------

def test_the_branch_location_filter_lists_the_branches_locations(client_in, world):
    post(client_in, "/company/branch/save/", branch_payload(world))

    screen = context_of(client_in, "/company/branch/list/")
    assert screen["branch_locations"] == ["Corniche"]
    page = client_in.get("/company/branch/list/").content.decode()
    assert 'data-value="Corniche"' in page and 'data-value="Abu Dhabi"' not in page


# -- BUG-019 / BUG-020: coordinates in range ---------------------------------------------

def test_coordinates_out_of_range_are_refused(client_in, world):
    response = post(client_in, "/company/branch/save/", branch_payload(world, latitude="200", longitude="200"))

    assert response.status_code == 400
    messages = {e["message"] for e in response.json()["errors"]}
    assert {"Latitude must be between -90 and 90.", "Longitude must be between -180 and 180."} <= messages
    assert not Branch.objects.exists()


def test_coordinates_in_range_are_saved(client_in, world):
    response = post(client_in, "/company/branch/save/", branch_payload(world, latitude="25.2048",
                                                                       longitude="-55.2708"))

    assert response.status_code == 200, response.content
    assert str(Branch.objects.get().latitude) == "25.204800"


# -- Hotel Commission is decided by Is Hotel ---------------------------------------------

def test_a_non_hotel_branch_saves_with_the_commission_box_empty(client_in, world):
    response = post(client_in, "/company/branch/save/", branch_payload(world, hotel_commission=""))

    assert response.status_code == 200, response.content
    assert Branch.objects.get().hotel_commission == 0


def test_a_non_hotel_branch_never_keeps_a_commission(client_in, world):
    post(client_in, "/company/branch/save/", branch_payload(world, hotel_commission="15"))

    assert Branch.objects.get().hotel_commission == 0


def test_a_hotel_needs_a_commission_between_0_and_100(client_in, world):
    empty = post(client_in, "/company/branch/save/", branch_payload(world, is_hotel=True, hotel_commission=""))
    too_much = post(client_in, "/company/branch/save/", branch_payload(world, is_hotel=True, hotel_commission="150"))

    assert empty.status_code == too_much.status_code == 400
    assert empty.json()["errors"][0]["message"] == "Enter the hotel commission."
    assert too_much.json()["errors"][0]["message"] == "Hotel commission must be between 0 and 100."


# -- Active switched off on Add is kept --------------------------------------------------

def test_active_switched_off_on_add_is_kept(client_in, world):
    post(client_in, "/company/department/save/", {"short_code": "OPS", "name": "Operations", "is_active": False})
    post(client_in, "/company/department/save/", {"short_code": "FIN", "name": "Finance"})

    assert Department.objects.get(short_code="OPS").is_active is False
    assert Department.objects.get(short_code="FIN").is_active is True
