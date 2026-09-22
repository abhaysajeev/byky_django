"""App Releases and App Mapping: the screens that feed the update check.

What the tablet is then told is design/registration/update-check-api.md; these
tests hold the screens to it -- the integer code only goes up (section 7.1),
Mandatory stops a tablet and Any time waits for working hours (section 4), a
withdrawn build falls back to All branches, and nothing is ever deleted.
"""

import datetime
import json

import pytest
from django.utils import timezone

from apps.company.models import BranchWorkingTime, Company
from apps.devices import scoping, services
from apps.devices.models import (
    AppRelease,
    AppReleaseMapping,
    Device,
    DeviceMapping,
    DeviceStatus,
    ReleaseScope,
    UpdateType,
)
from core.enums import Channel, UserScope
from core.models import User

OPERATOR = Channel.OPERATOR


@pytest.fixture
def admin(company):
    return User.objects.create_user(
        "admin.r", "Byky#2026", company=company, scope=UserScope.COMPANY,
        allowed_channels=[Channel.WEB],
    )


@pytest.fixture
def releases(admin):
    return scoping.releases_for(admin)


def add(admin, company, code, *, name=None, update_type=UpdateType.ANYTIME, channel=OPERATOR,
        url=None):
    return services.create_release(
        user=admin, company_id=company.pk, channel=channel,
        version_name=name or f"1.0.{code}", version_code=code, update_type=update_type,
        download_url=url or f"http://files.byky.test/op-{code}.apk",
    )


def mapped(releases, admin, release, *targets, branches=()):
    scope = ReleaseScope.BRANCH if targets else ReleaseScope.ALL
    return services.map_release(
        releases, release.pk, user=admin, scope=scope,
        branches=list(branches) or list(targets), targets=list(targets),
    )


def offered(company, branch=None):
    rel = services.release_for(
        company_id=company.pk, channel=OPERATOR, branch_id=branch.pk if branch else None,
    )
    return rel.version_code if rel else None


def hours(branch):
    for day in range(7):
        BranchWorkingTime.objects.create(
            branch=branch, week_day=day, shift_number=1,
            start_time=datetime.time(0, 0), end_time=datetime.time(23, 59),
        )


# -- Adding a release ----------------------------------------------------------------


def test_a_release_carries_what_the_update_check_sends(admin, company):
    rel = add(admin, company, 81, name="1.12.81", update_type=UpdateType.MANDATORY)

    assert (rel.version_name, rel.version_code, rel.is_mandatory) == ("1.12.81", 81, True)
    assert rel.created_by == admin
    assert not rel.mappings.exists()          # reaches no tablet until mapped


def test_every_problem_is_reported_at_once(admin, company):
    with pytest.raises(services.ReleaseInvalid) as refused:
        services.create_release(
            user=admin, company_id=company.pk, channel="web", version_name="1.0",
            version_code="x", update_type="sometimes", download_url="nope",
        )
    fields = {e["field"] for e in refused.value.errors}
    assert fields == {"App", "Version name", "Version code", "Update type", "Download link"}


def test_a_plain_http_link_is_accepted(admin, company):
    """Client decision: no https rule."""
    assert add(admin, company, 5, url="http://10.0.0.5/apk/op.apk").pk


def test_the_version_code_only_goes_up(admin, company):
    add(admin, company, 101)
    with pytest.raises(services.ReleaseInvalid, match="higher than 101"):
        add(admin, company, 100, name="1.0.200")
    with pytest.raises(services.ReleaseInvalid, match="higher than 101"):
        add(admin, company, 101, name="1.0.201")


def test_a_withdrawn_release_still_counts_for_the_code(admin, company, releases):
    rel = add(admin, company, 101)
    services.withdraw_release(releases, rel.pk, user=admin)
    with pytest.raises(services.ReleaseInvalid, match="higher than 101"):
        add(admin, company, 99)


def test_codes_rise_per_app(admin, company):
    add(admin, company, 101)
    assert add(admin, company, 5, channel=Channel.MANAGER).version_code == 5


def test_a_version_name_is_used_once_per_app(admin, company):
    add(admin, company, 101, name="1.0.1")
    with pytest.raises(services.ReleaseInvalid, match="already"):
        add(admin, company, 102, name="1.0.1")


# -- Editing, withdrawing -------------------------------------------------------------


def test_edit_changes_only_type_link_and_note(signed_in, company):
    rel = AppRelease.objects.create(
        company=company, channel=OPERATOR, version_name="1.0.1", version_code=101,
        download_url="http://a.test/1.apk",
    )
    response = signed_in.post("/devices/releases/save/", json.dumps({
        "pk": rel.pk, "channel": "manager", "version_name": "9.9.9", "version_code": "999",
        "update_type": "mandatory", "download_url": "http://b.test/1.apk", "update_note": "Fix",
    }), content_type="application/json")

    assert response.json()["ok"] is True
    rel.refresh_from_db()
    assert (rel.channel, rel.version_name, rel.version_code) == (OPERATOR, "1.0.1", 101)
    assert (rel.update_type, rel.download_url, rel.update_note) == ("mandatory", "http://b.test/1.apk", "Fix")


def test_withdraw_falls_back_to_all_branches_and_restore_returns(admin, company, branch, releases):
    everyone = add(admin, company, 101)
    dubai = add(admin, company, 102)
    mapped(releases, admin, everyone, branches=[branch])
    mapped(releases, admin, dubai, branch)
    assert offered(company, branch) == 102

    services.withdraw_release(releases, dubai.pk, user=admin)
    assert offered(company, branch) == 101

    services.restore_release(releases, dubai.pk, user=admin)
    assert offered(company, branch) == 102


def test_withdraw_says_where_branches_go(admin, company, branch, releases):
    everyone = add(admin, company, 101)
    dubai = add(admin, company, 102)
    mapped(releases, admin, everyone, branches=[branch])
    mapped(releases, admin, dubai, branch)

    assert services.withdraw_consequence(dubai) == (
        "1 branch mapped to it falls back to 1.0.101 (101), the All-branches release."
    )
    assert "offered no update" in services.withdraw_consequence(everyone)


def test_a_withdrawn_release_cannot_be_mapped(admin, company, branch, releases):
    rel = add(admin, company, 101)
    services.withdraw_release(releases, rel.pk, user=admin)
    with pytest.raises(services.DeviceActionError, match="withdrawn"):
        mapped(releases, admin, rel, branch)


# -- Mapping -----------------------------------------------------------------------------


def test_remapping_a_branch_keeps_the_old_row_as_history(admin, company, branch, releases):
    first = add(admin, company, 101)
    second = add(admin, company, 102)
    mapped(releases, admin, first, branch)
    mapped(releases, admin, second, branch)

    rows = AppReleaseMapping.objects.filter(branch=branch).order_by("pk")
    assert [(r.release_id, r.is_active) for r in rows] == [(first.pk, False), (second.pk, True)]
    assert rows[0].modified_by == admin


def test_a_new_all_row_replaces_the_old_one(admin, company, releases):
    first = add(admin, company, 101)
    second = add(admin, company, 102)
    mapped(releases, admin, first)
    mapped(releases, admin, second)

    active = AppReleaseMapping.objects.filter(scope=ReleaseScope.ALL, is_active=True)
    assert [m.release_id for m in active] == [second.pk]
    assert AppReleaseMapping.objects.filter(scope=ReleaseScope.ALL).count() == 2


def test_mapping_the_same_build_twice_is_refused(admin, company, branch, releases):
    rel = add(admin, company, 101)
    mapped(releases, admin, rel, branch)
    with pytest.raises(services.DeviceActionError, match="already on"):
        mapped(releases, admin, rel, branch)
    mapped(releases, admin, rel)
    with pytest.raises(services.DeviceActionError, match="already the All-branches"):
        mapped(releases, admin, rel)


def test_another_companys_branch_is_refused(admin, company, releases, branch):
    other = Company.objects.create(
        short_code="TWO", name="Second", country=company.country, state=company.state,
        phone_number="+9712222222", email="two@byky.test",
    )
    branch.company = other
    rel = add(admin, company, 101)
    with pytest.raises(services.DeviceActionError, match="this company"):
        mapped(releases, admin, rel, branch)


def test_remove_falls_back_to_all_branches(admin, company, branch, releases):
    everyone = add(admin, company, 101)
    dubai = add(admin, company, 102)
    mapped(releases, admin, everyone, branches=[branch])
    mapped(releases, admin, dubai, branch)
    own = AppReleaseMapping.objects.get(branch=branch, is_active=True)

    services.unmap_release(scoping.release_mappings_for(admin), own.pk, user=admin)

    assert offered(company, branch) == 101
    with pytest.raises(services.DeviceActionError, match="already removed"):
        services.unmap_release(scoping.release_mappings_for(admin), own.pk, user=admin)


# -- The preview ---------------------------------------------------------------------------


def test_preview_counts_and_warns_on_a_mandatory_build(admin, company, branch, other_branch, releases):
    everyone = add(admin, company, 101)
    mapped(releases, admin, everyone, branches=[branch, other_branch])
    urgent = add(admin, company, 102, update_type=UpdateType.MANDATORY)

    plan = services.mapping_plan(company.pk, OPERATOR, urgent, ReleaseScope.BRANCH,
                                 [branch, other_branch], [branch])

    assert plan["summary"] == "1 branch moves to 1.0.102 (102) · 1 unchanged (All branches: 1.0.101 (101))"
    assert [w["code"] for w in plan["warnings"]] == ["mandatory"]


def test_preview_warns_when_a_branch_would_get_an_older_build(admin, company, branch, other_branch, releases):
    old = add(admin, company, 101)
    new = add(admin, company, 102)
    mapped(releases, admin, new, branches=[branch, other_branch])
    hours(branch)

    plan = services.mapping_plan(company.pk, OPERATOR, old, ReleaseScope.BRANCH,
                                 [branch, other_branch], [branch])

    assert [w["code"] for w in plan["warnings"]] == ["older"]


def test_preview_warns_an_any_time_build_waits_for_working_time(admin, company, branch, other_branch):
    hours(branch)
    rel = add(admin, company, 101)

    plan = services.mapping_plan(company.pk, OPERATOR, rel, ReleaseScope.ALL,
                                 [branch, other_branch], [])

    assert plan["summary"] == "2 branches move to 1.0.101 (101)"
    warning = plan["warnings"][0]
    assert warning["code"] == "working_time"
    assert "1 branch has no working time" in warning["message"]
    assert other_branch.name in warning["message"] and branch.name not in warning["message"]


# -- Endpoints ----------------------------------------------------------------------------


def post(client, url, payload=None):
    return client.post(url, json.dumps(payload or {}), content_type="application/json")


def test_saving_past_a_warning_needs_confirmation(signed_in, company, branch):
    rel = AppRelease.objects.create(
        company=company, channel=OPERATOR, version_name="1.0.1", version_code=101,
        update_type=UpdateType.MANDATORY, download_url="http://a.test/1.apk",
    )
    body = {"release": rel.pk, "scope": "branch", "branches": [branch.pk]}

    refused = post(signed_in, "/devices/release-mapping/save/", body)
    assert (refused.status_code, refused.json()["code"]) == (400, "confirm")
    assert not AppReleaseMapping.objects.exists()

    assert post(signed_in, "/devices/release-mapping/save/", {**body, "confirmed": True}).json()["ok"]
    assert AppReleaseMapping.objects.get().branch == branch


def test_the_preview_endpoint_writes_nothing(signed_in, company, branch):
    rel = AppRelease.objects.create(
        company=company, channel=OPERATOR, version_name="1.0.1", version_code=101,
        download_url="http://a.test/1.apk",
    )
    reply = post(signed_in, "/devices/release-mapping/preview/", {"release": rel.pk, "scope": "all"}).json()

    assert reply["ok"] and reply["plan"]["moving"] == 1
    assert not AppReleaseMapping.objects.exists()


def test_the_drawer_adds_a_release_for_the_users_company(signed_in, company):
    reply = post(signed_in, "/devices/releases/save/", {
        "channel": "operator", "version_name": "1.0.1", "version_code": "101",
        "update_type": "mandatory", "download_url": "http://a.test/1.apk", "update_note": "First",
    }).json()

    assert reply["ok"] is True
    assert AppRelease.objects.get().company == company


@pytest.mark.parametrize("url", [
    "/devices/releases/save/",
    "/devices/releases/1/withdraw/",
    "/devices/releases/1/restore/",
    "/devices/release-mapping/preview/",
    "/devices/release-mapping/save/",
    "/devices/release-mapping/1/remove/",
])
def test_every_write_needs_permission(no_permissions, url):
    assert post(no_permissions, url).status_code == 403


# -- Screens -------------------------------------------------------------------------------


def test_both_screens_are_in_the_devices_sidebar(signed_in):
    html = signed_in.get("/devices/releases/").content.decode()
    order = [html.index(f'href="/devices/{p}/"') for p in ("mapping", "releases", "release-mapping")]
    assert order == sorted(order)


def test_the_release_screen_shows_the_update_type(signed_in, company):
    AppRelease.objects.create(
        company=company, channel=OPERATOR, version_name="1.0.1", version_code=101,
        update_type=UpdateType.MANDATORY, download_url="http://a.test/1.apk",
    )
    html = signed_in.get("/devices/releases/").content.decode()

    assert "scr-badge-mandatory" in html
    assert 'data-field="update_type"' in html and "Any time" in html
    assert 'data-field="version_code"' in html and 'data-lock-on-edit="true"' in html
    assert 'data-field="is_active"' not in html          # withdraw, not an Active switch
    assert f"Company: {company.name}" in html


def test_the_mapping_screen_shows_each_branch_and_its_source(signed_in, company, branch, other_branch):
    rel = AppRelease.objects.create(
        company=company, channel=OPERATOR, version_name="1.0.1", version_code=101,
        download_url="http://a.test/1.apk",
    )
    AppReleaseMapping.objects.create(release=rel, company=company, channel=OPERATOR,
                                     scope=ReleaseScope.BRANCH, branch=branch)
    hours(branch)
    html = signed_in.get("/devices/release-mapping/").content.decode()

    assert f'data-source="own" data-hours="set" data-branch="{branch.pk}"' in html
    assert f'data-source="none" data-hours="unset" data-branch="{other_branch.pk}"' in html
    assert "No default release" in html
    assert "byky-app-mapping.js" in html


# -- End to end, as the tablet sees it -------------------------------------------------------


def update_check(client, installation_id, code):
    return client.post("/api/v1/operator/app/update-check", {
        "credentials": {}, "request_data": {"installation_id": installation_id, "version_code": code},
    }, content_type="application/json").json()


def test_what_the_screens_map_is_what_a_tablet_is_told(signed_in, client, company, branch):
    device = Device.objects.create(
        company=company, installation_id="tab-1", platform="android", channel=OPERATOR,
        status=DeviceStatus.APPROVED, name="POS-1", approved_at=timezone.now(),
    )
    DeviceMapping.objects.create(device=device, branch=branch, from_date=device.created_on)

    add_body = {"channel": "operator", "version_name": "1.0.1", "version_code": "101",
                "update_type": "mandatory", "download_url": "http://a.test/1.apk"}
    pk = post(signed_in, "/devices/releases/save/", add_body).json()["pk"]
    post(signed_in, "/devices/release-mapping/save/",
         {"release": pk, "scope": "all", "confirmed": True})

    reply = update_check(client, "tab-1", 100)
    assert reply["code"] == "update_required"
    assert reply["data"]["version_code"] == 101

    # Any time, and the branch has no working time: held back, not an error.
    post(signed_in, "/devices/releases/save/", {"pk": pk, "update_type": "anytime",
                                               "download_url": "http://a.test/1.apk"})
    assert update_check(client, "tab-1", 100)["code"] == "working_time_not_set"
