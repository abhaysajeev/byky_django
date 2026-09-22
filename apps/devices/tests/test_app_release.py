"""App releases, which branches get them, and what a tablet is told.

The client's own words set the central case: "today I release and say all
branches; tomorrow I release and select only Dubai -- that build goes to Dubai
devices only, and the rest keep yesterday's all-branches release. If the Dubai
mapping is made inactive, Dubai falls back to all."

In these tests `branch` plays Dubai and `other_branch` plays Sharjah.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.devices import services
from apps.devices.models import (
    AppRelease,
    AppReleaseMapping,
    Device,
    DeviceMapping,
    DeviceStatus,
    ReleaseScope,
    UpdateType,
)
from core.enums import Channel

OPERATOR = Channel.OPERATOR


def release(company, code, *, name=None, channel=OPERATOR, update_type=UpdateType.ANYTIME, active=True):
    return AppRelease.objects.create(
        company=company, channel=channel,
        version_name=name or f"1.12.{code}", version_code=code,
        update_type=update_type,
        download_url=f"https://files.byky.test/apk/operator-1.12.{code}.apk",
        is_active=active,
    )


def map_to(rel, branch=None, *, active=True):
    """A mapping, with company and channel copied from the release as the
    mapping service will do."""
    return AppReleaseMapping.objects.create(
        release=rel, company_id=rel.company_id, channel=rel.channel,
        scope=ReleaseScope.BRANCH if branch else ReleaseScope.ALL,
        branch=branch, is_active=active,
    )


def resolved(company, branch=None, channel=OPERATOR):
    rel = services.release_for(
        company_id=company.pk, channel=channel, branch_id=branch.pk if branch else None
    )
    return rel.version_code if rel else None


def immediate_constraints():
    """The composite foreign key is DEFERRABLE INITIALLY DEFERRED, so it would
    only fire at commit -- which a test transaction never reaches."""
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


# -- The release catalogue --------------------------------------------------


def test_a_version_code_is_used_once_per_app(company):
    release(company, 80)
    with pytest.raises(IntegrityError):
        release(company, 80, name="1.12.800")


def test_the_same_code_may_exist_for_a_different_app(company):
    release(company, 80)
    release(company, 80, channel=Channel.MANAGER)  # no error

    assert AppRelease.objects.filter(version_code=80).count() == 2


def test_a_version_name_is_used_once_per_app(company):
    """The legacy had 1.12.79 twice, and two rows claiming 1.12.77 and 1.12.78
    that both pointed at the 1.12.68 binary."""
    release(company, 80)
    with pytest.raises(IntegrityError):
        release(company, 81, name="1.12.80")


@pytest.mark.parametrize("bad", ["1.12", "Version 1.12.80", "1.12.80-rc1", "v1.12.80"])
def test_a_version_name_is_digits_and_dots(company, bad):
    rel = AppRelease(
        company=company, channel=OPERATOR, version_name=bad, version_code=80,
        download_url="https://files.byky.test/a.apk",
    )
    with pytest.raises(ValidationError):
        rel.full_clean()


def test_there_is_no_release_for_the_web(company):
    with pytest.raises(IntegrityError):
        release(company, 80, channel=Channel.WEB)


def test_a_version_code_is_positive(company):
    with pytest.raises(IntegrityError):
        release(company, 0, name="1.0.0")


def test_mandatory_is_read_from_the_dropdown(company):
    assert release(company, 80, update_type=UpdateType.MANDATORY).is_mandatory is True
    assert release(company, 81, update_type=UpdateType.ANYTIME).is_mandatory is False


# -- Mappings ---------------------------------------------------------------


def test_an_all_mapping_carries_no_branch(company, branch):
    rel = release(company, 80)
    with pytest.raises(IntegrityError):
        AppReleaseMapping.objects.create(
            release=rel, company=company, channel=OPERATOR,
            scope=ReleaseScope.ALL, branch=branch,
        )


def test_a_branch_mapping_needs_a_branch(company):
    rel = release(company, 80)
    with pytest.raises(IntegrityError):
        AppReleaseMapping.objects.create(
            release=rel, company=company, channel=OPERATOR, scope=ReleaseScope.BRANCH,
        )


def test_a_branch_has_one_active_mapping_per_app(company, branch):
    map_to(release(company, 80), branch)
    with pytest.raises(IntegrityError):
        map_to(release(company, 81), branch)


def test_there_is_one_active_all_mapping_per_app(company):
    """The nulls_distinct=False test. An All row has no branch, and by default
    two NULLs never collide -- so without that flag both rows would exist."""
    map_to(release(company, 80))
    with pytest.raises(IntegrityError):
        map_to(release(company, 81))


def test_a_branch_may_carry_one_mapping_per_app(company, branch):
    map_to(release(company, 80), branch)
    map_to(release(company, 80, channel=Channel.MANAGER), branch)  # no error

    assert AppReleaseMapping.objects.filter(branch=branch, is_active=True).count() == 2


def test_mapping_history_can_repeat_the_same_pair(company, branch):
    """Map, unmap, re-map: three rows, one active. A unique (release, branch)
    would refuse the re-map, which is why there isn't one."""
    rel = release(company, 80)
    for _ in range(3):
        AppReleaseMapping.objects.filter(release=rel, is_active=True).update(is_active=False)
        map_to(rel, branch)

    assert AppReleaseMapping.objects.filter(release=rel, branch=branch).count() == 3
    assert AppReleaseMapping.objects.filter(release=rel, is_active=True).count() == 1


def test_a_mapping_cannot_disagree_with_its_release(company, branch):
    """The composite foreign key. Its copied channel must match the release's."""
    rel = release(company, 80)
    immediate_constraints()
    with pytest.raises(IntegrityError), transaction.atomic():
        AppReleaseMapping.objects.create(
            release=rel, company=company, channel=Channel.MANAGER,
            scope=ReleaseScope.BRANCH, branch=branch,
        )


def test_a_mapped_release_cannot_change_its_app(company, branch):
    rel = release(company, 80)
    map_to(rel, branch)
    immediate_constraints()
    with pytest.raises(IntegrityError), transaction.atomic():
        AppRelease.objects.filter(pk=rel.pk).update(channel=Channel.MANAGER)


# -- The lookup -------------------------------------------------------------


def test_the_branch_mapping_beats_all_branches(company, branch):
    map_to(release(company, 80))
    map_to(release(company, 81), branch)

    assert resolved(company, branch) == 81


def test_an_unmapped_branch_gets_all_branches(company, branch, other_branch):
    map_to(release(company, 80))
    map_to(release(company, 81), branch)

    assert resolved(company, other_branch) == 80


def test_with_no_all_mapping_an_unmapped_branch_gets_nothing(company, other_branch):
    """No guessing: the legacy fell back to the newest catalogue row, which
    offered every unmapped phone whatever had just been uploaded."""
    release(company, 99)  # uploaded, never mapped
    assert resolved(company, other_branch) is None


def test_an_uploaded_release_reaches_nobody_until_mapped(company, branch):
    map_to(release(company, 80))
    release(company, 82)  # newer, active, unmapped

    assert resolved(company, branch) == 80


def test_a_withdrawn_release_falls_through_to_all_branches(company, branch):
    """The critical one. Withdrawing v81 must not strand Dubai with nothing."""
    map_to(release(company, 80))
    v81 = release(company, 81)
    map_to(v81, branch)

    v81.is_active = False
    v81.save(update_fields=["is_active"])

    assert resolved(company, branch) == 80


def test_withdrawing_the_all_release_leaves_nothing(company, other_branch):
    v80 = release(company, 80)
    map_to(v80)
    v80.is_active = False
    v80.save(update_fields=["is_active"])

    assert resolved(company, other_branch) is None


def test_reactivating_a_release_restores_its_mappings(company, branch):
    map_to(release(company, 80))
    v81 = release(company, 81, active=False)
    map_to(v81, branch)
    assert resolved(company, branch) == 80

    v81.is_active = True
    v81.save(update_fields=["is_active"])

    assert resolved(company, branch) == 81


def test_another_apps_mapping_never_resolves(company, branch):
    map_to(release(company, 90, channel=Channel.MANAGER), branch)

    assert resolved(company, branch, channel=OPERATOR) is None


# -- The client's scenario --------------------------------------------------


def test_the_client_scenario_end_to_end(company, branch, other_branch):
    dubai, sharjah = branch, other_branch

    # Day 1: v80 to all branches.
    map_to(release(company, 80))
    assert (resolved(company, dubai), resolved(company, sharjah)) == (80, 80)

    # Day 2: v81 to Dubai only.
    dubai_row = map_to(release(company, 81), dubai)
    assert (resolved(company, dubai), resolved(company, sharjah)) == (81, 80)

    # Day 3: the Dubai mapping switched off -- Dubai falls back to all.
    dubai_row.is_active = False
    dubai_row.save(update_fields=["is_active"])
    assert (resolved(company, dubai), resolved(company, sharjah)) == (80, 80)


# -- What the tablet is told ------------------------------------------------


def decision(company, branch=None, **version):
    return services.update_decision(
        company_id=company.pk, channel=OPERATOR,
        branch_id=branch.pk if branch else None, **version,
    )


def test_an_older_device_is_told_to_update(company):
    map_to(release(company, 81, update_type=UpdateType.MANDATORY))

    answer = decision(company, version_code=80)

    assert answer["update_required"] is True
    assert answer["is_mandatory"] is True
    assert answer["version_code"] == 81
    assert answer["download_url"].startswith("https://")


def test_an_optional_update_is_not_mandatory(company):
    map_to(release(company, 81, update_type=UpdateType.ANYTIME))

    assert decision(company, version_code=80)["is_mandatory"] is False


@pytest.mark.parametrize("installed", [81, 82])
def test_a_current_or_newer_device_is_left_alone(company, installed):
    map_to(release(company, 81, update_type=UpdateType.MANDATORY))

    assert decision(company, version_code=installed) == {"update_required": False}


def test_no_update_carries_no_mandatory_flag(company):
    """A reply of "no update" and "mandatory" together would trap the app."""
    map_to(release(company, 81, update_type=UpdateType.MANDATORY))

    assert decision(company, version_code=81) == {"update_required": False}


def test_a_rollback_never_downgrades(company):
    """v80 mapped to all while a device already runs v82. Android will not
    install an older build over a newer one, so telling it to would lock the
    station out. It is left alone, even though v80 is mandatory."""
    map_to(release(company, 80, update_type=UpdateType.MANDATORY))

    assert decision(company, version_code=82) == {"update_required": False}
    assert decision(company, version_code=79)["update_required"] is True


def test_without_a_code_the_name_is_compared_as_numbers(company):
    """As text, 1.12.9 sorts after 1.12.10."""
    map_to(release(company, 10, name="1.12.10"))

    assert decision(company, version_name="1.12.9")["update_required"] is True
    assert decision(company, version_name="1.12.10") == {"update_required": False}


def test_a_version_prefix_is_tolerated(company):
    """Seven variants of 'Version x.y.z' appear in the legacy's login records."""
    map_to(release(company, 80, name="1.12.80"))

    assert decision(company, version_name="Version 1.12.76")["update_required"] is True


def test_an_unreadable_version_fails_open(company):
    map_to(release(company, 80))

    assert decision(company, version_name="unknown") == {"update_required": False}
    assert decision(company) == {"update_required": False}


def test_any_error_fails_open(company, monkeypatch):
    """A version check must never be what stops every station working."""
    def broken(**kwargs):
        raise RuntimeError("database away")

    monkeypatch.setattr(services, "release_for", broken)

    assert decision(company, version_code=1) == {"update_required": False}


# -- By installation id, before login ---------------------------------------


def tablet(company, installation_id, *, status=DeviceStatus.APPROVED, station=None):
    device = Device.objects.create(
        company=company, installation_id=installation_id, platform="android",
        channel=OPERATOR, status=status,
        approved_at=timezone.now() if status == DeviceStatus.APPROVED else None,
    )
    if station is not None:
        DeviceMapping.objects.create(device=device, branch=station, from_date=timezone.localdate())
    return device


def open_all_day(*branches):
    """00:00-23:59 every day. An "Any time" release is only offered while the
    branch is open (03-login.md section 9A.3a); tests about which release a
    tablet gets are not about hours, so their stations are simply open."""
    from apps.company.models import BranchWorkingTime

    for branch in branches:
        for day in range(7):
            BranchWorkingTime.objects.create(
                branch=branch, week_day=day, shift_number=1,
                start_time="00:00", end_time="23:59",
            )


def check(installation_id, version_code):
    return services.update_decision_for_installation(
        installation_id=installation_id, channel=OPERATOR, version_code=version_code,
    )


def test_a_known_tablet_gets_its_stations_release(company, branch):
    map_to(release(company, 80))
    map_to(release(company, 81), branch)
    tablet(company, "dubai-tablet", station=branch)
    open_all_day(branch)

    assert check("dubai-tablet", 80)["version_code"] == 81


def test_a_tablet_not_placed_at_a_station_gets_all_branches(company, branch):
    # Mandatory: a tablet with no station has no working hours to wait for.
    map_to(release(company, 80, update_type=UpdateType.MANDATORY))
    map_to(release(company, 81, update_type=UpdateType.MANDATORY), branch)
    tablet(company, "unplaced-tablet")

    assert check("unplaced-tablet", 70)["version_code"] == 80


def test_an_unknown_tablet_gets_the_companys_all_branches(company):
    """First launch: the server has never heard of it. The company is this
    deployment's own; the app is never trusted to name one."""
    map_to(release(company, 80, update_type=UpdateType.MANDATORY))

    assert check("never-seen-before", 70)["version_code"] == 80


def test_a_pending_tablet_can_still_update(company):
    """Otherwise a new tablet on an old build could never reach login to
    register."""
    map_to(release(company, 80, update_type=UpdateType.MANDATORY))
    tablet(company, "pending-tablet", status=DeviceStatus.PENDING)

    assert check("pending-tablet", 70)["update_required"] is True
