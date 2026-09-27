"""The fare screens and endpoints: who may, what they see, what comes back."""



from apps.fare.models import Fare
from apps.fare.tests.conftest import D, make_fare, make_rule, make_season, post
from apps.portal.models import RolePermission

SAVE, CHECK = "/fare/save/", "/fare/check/"


def revoke(client, *actions, page="fare.fare"):
    RolePermission.objects.filter(role=client.role, page__code=page).update(**{f"can_{a}": False for a in actions})


def fare_data(world, **changes):
    price = {"base_fare": "50", "grace_minutes": 5, "concurrent_interval_minutes": 10,
             "concurrent_fare": "10", "concurrent_grace_minutes": 0}
    values = {"vehicle_type": world["monaco"].pk, "level": "company", "branches": [],
              "valid_from": "2026-01-01", "valid_to": "2026-12-31", "is_active": True,
              "package_minutes": 30, "base": price, "rules": [], "seasons": []}
    values.update(changes)
    return values


# -- Screens ------------------------------------------------------------------------


def test_the_list_shows_this_companys_fares_only(client_in, world):
    make_fare(world, branches=[world["adc1"]])
    make_fare(world, company=world["other"], vehicle_type=world["their_type"])
    html = client_in.get("/fare/list/").content.decode()
    assert "Monaco · 30 min" in html and "1 branch" in html
    assert "Their Kart" not in html


def test_the_list_has_a_vehicle_search_and_a_branch_search(client_in, world):
    make_fare(world)                                                  # company: every branch
    make_fare(world, vehicle_type=world["berg"], branches=[world["adc1"]])
    html = client_in.get("/fare/list/").content.decode()
    assert 'data-search-key="vehicle"' in html and 'data-search-key="branch"' in html
    company_row = html[html.index('data-search-vehicle="monaco'):]
    company_row = company_row[:company_row.index(">")]
    assert "abu dhabi corniche 1" in company_row and "abu dhabi corniche 2" in company_row
    branch_row = html[html.index('data-search-vehicle="berg'):]
    branch_row = branch_row[:branch_row.index(">")]
    assert 'data-search-branch="abu dhabi corniche 1"' in branch_row


def test_save_sits_beside_test_fare_and_a_saved_fare_is_locked(client_in, world):
    html = client_in.get("/fare/add/").content.decode()
    head = html[html.index('class="scr-head-actions"'):html.index("data-fare-banner")]
    assert "data-fare-test-open" in head and "data-fare-save" in head
    fare = make_fare(world)
    html = client_in.get(f"/fare/{fare.pk}/edit/").content.decode()
    assert "Fixed once saved" in html
    for field in ('data-fare-field="vehicle_type" disabled', 'data-fare-category disabled'):
        assert field in html


def test_the_add_page_needs_create(client_in):
    revoke(client_in, "create")
    assert client_in.get("/fare/add/").status_code == 403


def test_the_edit_page_carries_the_saved_fare(client_in, world):
    fare = make_fare(world)
    season = make_season(fare, D(2026, 4, 10), D(2026, 5, 15))
    make_rule(fare, season=season)
    html = client_in.get(f"/fare/{fare.pk}/edit/").content.decode()
    assert 'id="fare-initial"' in html and f'"pk": {fare.pk}' in html
    assert '"name": "Spring"' in html


def test_without_update_the_edit_page_is_read_only(client_in, world):
    fare = make_fare(world)
    revoke(client_in, "update")
    html = client_in.get(f"/fare/{fare.pk}/edit/").content.decode()
    assert "Read only" in html and "data-fare-save" not in html


def test_another_companys_fare_is_not_found(client_in, world):
    theirs = make_fare(world, company=world["other"], vehicle_type=world["their_type"])
    assert client_in.get(f"/fare/{theirs.pk}/edit/").status_code == 404
    assert post(client_in, f"/fare/{theirs.pk}/delete/", {}).status_code == 404


# -- Save ------------------------------------------------------------------------------


def test_save_creates_a_fare(client_in, world):
    body = post(client_in, SAVE, fare_data(world)).json()
    assert body["ok"] is True and body["message"] == "Fare saved."
    assert Fare.objects.get(pk=body["pk"]).company == world["company"]


def test_save_lists_every_problem(client_in, world):
    response = post(client_in, SAVE, fare_data(world, vehicle_type=None, valid_to="2025-01-01"))
    assert response.status_code == 400
    fields = [e["field"] for e in response.json()["errors"]]
    assert "Vehicle type" in fields and "Valid to" in fields


def test_save_refuses_a_stale_copy(client_in, world):
    fare = make_fare(world)
    response = post(client_in, SAVE, fare_data(world, pk=fare.pk, lock_version=7))
    assert response.status_code == 409
    assert response.json()["code"] == "stale"


def test_save_needs_the_right_permission(client_in, world):
    revoke(client_in, "create")
    assert post(client_in, SAVE, fare_data(world)).status_code == 403
    fare = make_fare(world)
    revoke(client_in, "update")
    assert post(client_in, SAVE, fare_data(world, pk=fare.pk)).status_code == 403


# -- Check -----------------------------------------------------------------------------


def test_check_answers_test_fare_for_an_unsaved_fare(client_in, world):
    response = post(client_in, CHECK, {"fare": fare_data(world), "test": {"date": "2026-06-01", "time": "10:00"}})
    body = response.json()
    assert body["ok"] is True
    assert body["test"]["price"]["base_fare"] == "50.00"
    assert body["test"]["source"]["label"] == "Base fare"
    assert not Fare.objects.exists()


def test_check_needs_read_only(client_in, world):
    revoke(client_in, "create", "update", "delete")
    assert post(client_in, CHECK, {"fare": fare_data(world)}).status_code == 200
    revoke(client_in, "read")
    assert post(client_in, CHECK, {"fare": fare_data(world)}).status_code == 403


# -- Delete ------------------------------------------------------------------------------


def test_delete_removes_the_fare_and_its_rows(client_in, world):
    fare = make_fare(world, branches=[world["adc1"]])
    make_rule(fare)
    body = post(client_in, f"/fare/{fare.pk}/delete/", {}).json()
    assert body["ok"] is True
    assert not Fare.objects.exists()
