"""The Device Settings screen: one station's receipt, printer and prefix.

design/03-login.md section 8.7. The tablet downloads this after login and keeps
it, logo and all, so a station can trade with no network -- which is why the
bitmap is stored as bytes and travels in the payload rather than behind a link,
exactly as the legacy sent it (Service_Get_Device_SEttings.sql:46-50).
"""

import base64
import datetime
import json
import struct

import pytest
from django.utils import timezone

from apps.company.scoping import branches_for
from apps.devices import scoping, services
from apps.devices.models import DeviceSettings, PrintType, RoundOffMode
from core.enums import ApprovalStatus, Channel, UserScope
from core.models import User


@pytest.fixture
def admin(company):
    return User.objects.create_user(
        "admin.s", "Byky#2026", company=company, scope=UserScope.COMPANY,
        allowed_channels=[Channel.WEB],
    )


@pytest.fixture
def rows(admin):
    return scoping.settings_for(admin)


@pytest.fixture
def stations(admin):
    from apps.company.scoping import branches_for
    return branches_for(admin)


def form(branch=None, **extra):
    fields = {
        "settings_code": "ZAKHER1",
        "order_no_prefix": "AZP01",
        "header_1": "AL ZAKHER PARK",
        "header_2": "AL AIN",
        "additional_header_1": "BYKY SPORTS AND LEISURE EQUIPMENT LLC",
        "footer_1": "Company is not responsible for personal belongings",
        "footer_2_arabic": "الشركة غير مسؤولة عن فقدان الأغراض الشخصية",
        "paper_feed": "2",
        "receipt_copies": "1",
        "print_type": PrintType.LANDSCAPE,
        "print_logo": True,
        "round_off_mode": RoundOffMode.UPWARD,
        "round_off_step": "0.25",
        "customer_test_minutes": "5",
        "cashier_test_minutes": "5",
    }
    if branch is not None:
        fields["branch"] = branch.pk
    fields.update(extra)
    return fields


def save(rows, stations, admin, branch=None, pk=None, **extra):
    return services.save_settings(
        rows, stations, user=admin, pk=pk, data=form(branch, **extra),
    )


def bmp(width=832, height=8, bits=1):
    """A bitmap shaped like the ones on the client's printers: 1-bit, and as
    wide as the 80 mm print head."""
    row = (width * bits + 31) // 32 * 4
    pixels = bytes([0b10101010] * row) * height
    palette = bytes([0, 0, 0, 0, 255, 255, 255, 0]) if bits == 1 else b""
    offset = 14 + 40 + len(palette)
    header = b"BM" + struct.pack("<IHHI", offset + len(pixels), 0, 0, offset)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, bits, 0,
                      len(pixels), 2835, 2835, 2 if bits == 1 else 0, 0)
    return header + dib + palette + pixels


# -- Saving -------------------------------------------------------------------


def test_a_station_gets_its_receipt_printer_and_prefix(rows, stations, admin, branch):
    row = save(rows, stations, admin, branch)

    assert row.branch == branch
    assert (row.order_no_prefix, row.paper_feed) == ("AZP01", 2)
    assert row.is_active and row.approval_status == ApprovalStatus.APPROVED
    assert services.bill_prefix_for(branch) == "AZP01"


def test_every_problem_is_reported_at_once(rows, stations, admin):
    with pytest.raises(services.Invalid) as refused:
        services.save_settings(rows, stations, user=admin, data={
            "settings_code": "", "order_no_prefix": "toolong!", "paper_feed": "44",
            "receipt_copies": "0", "customer_test_minutes": "0", "round_off_step": "0.30",
        })
    fields = {e["field"] for e in refused.value.errors}
    assert fields == {
        "Station", "Settings Code", "Order No Starting Characters", "Header 1", "Header 2",
        "Footer 1", "Paper Feed", "No of Receipt Copy", "Customer Test Time Slot",
        "Cashier Test Time Slot", "Round Off Limit",
    }


def test_a_station_keeps_one_set_of_settings(rows, stations, admin, branch):
    save(rows, stations, admin, branch)
    with pytest.raises(services.Invalid, match="already has settings"):
        save(rows, stations, admin, branch, settings_code="OTHER", order_no_prefix="OTHER")


def test_two_stations_cannot_print_the_same_prefix(rows, stations, admin, branch, other_branch):
    save(rows, stations, admin, branch)
    with pytest.raises(services.Invalid, match="already prints with AZP01"):
        save(rows, stations, admin, other_branch, settings_code="SECOND")


def test_the_prefix_is_stored_upper_case(rows, stations, admin, branch):
    assert save(rows, stations, admin, branch, order_no_prefix="azp01").order_no_prefix == "AZP01"


def test_a_settings_code_belongs_to_one_station(rows, stations, admin, branch, other_branch):
    save(rows, stations, admin, branch)
    with pytest.raises(services.Invalid, match="already used"):
        save(rows, stations, admin, other_branch, order_no_prefix="OTHER")


def test_editing_never_moves_settings_to_another_station(rows, stations, admin, branch, other_branch):
    row = save(rows, stations, admin, branch)

    changed = save(rows, stations, admin, other_branch, pk=row.pk, footer_1="New wording")

    assert changed.branch == branch
    assert changed.footer_1 == "New wording"
    assert changed.modified_by == admin


# -- The logo ------------------------------------------------------------------


def test_the_logo_is_stored_byte_for_byte(rows, stations, admin, branch):
    row = save(rows, stations, admin, branch)
    raw = bmp()

    saved, warnings = services.set_logo(rows, row.pk, user=admin, raw=raw, name="ZAKHER.bmp")

    assert warnings == []
    stored = DeviceSettings.objects.with_logo().get(pk=row.pk)
    assert bytes(stored.logo) == raw          # never re-encoded: the print head gets these bytes
    assert (stored.logo_name, stored.logo_changed_on is not None) == ("ZAKHER.bmp", True)


def test_an_odd_bitmap_is_stored_with_a_warning_not_refused(rows, stations, admin, branch):
    """The client's own live data has a 32-bit 100x100 BMP in it."""
    row = save(rows, stations, admin, branch)

    _, warnings = services.set_logo(rows, row.pk, user=admin, raw=bmp(100, 100, bits=32))

    assert any("1-bit" in w for w in warnings)
    assert DeviceSettings.objects.with_logo().get(pk=row.pk).logo


def test_a_wide_bitmap_says_it_will_be_cut_off(rows, stations, admin, branch):
    row = save(rows, stations, admin, branch)
    _, warnings = services.set_logo(rows, row.pk, user=admin, raw=bmp(1200))
    assert any("832" in w for w in warnings)


def test_something_that_is_not_a_bitmap_is_named_as_such(rows, stations, admin, branch):
    row = save(rows, stations, admin, branch)
    _, warnings = services.set_logo(rows, row.pk, user=admin, raw=b"\x89PNG\r\n\x1a\n rest")
    assert warnings == ["This is not a BMP. The printers take a bitmap; anything else may not print."]


def test_a_huge_file_is_refused(rows, stations, admin, branch):
    row = save(rows, stations, admin, branch)
    with pytest.raises(services.DeviceActionError, match="under"):
        services.set_logo(rows, row.pk, user=admin, raw=b"BM" + b"0" * services.LOGO_MAX_BYTES)


def test_removing_the_logo_is_a_change_the_tablet_hears_about(rows, stations, admin, branch):
    row = save(rows, stations, admin, branch)
    services.set_logo(rows, row.pk, user=admin, raw=bmp())

    cleared = services.clear_logo(rows, row.pk, user=admin)

    assert not DeviceSettings.objects.with_logo().get(pk=row.pk).logo
    assert cleared.logo_name == ""
    assert cleared.logo_changed_on is not None      # so the tablet drops its copy


def test_a_list_query_does_not_carry_the_bitmaps(rows, stations, admin, branch, django_assert_num_queries):
    """95 stations at 25 KB each is 2 MB a grid never shows."""
    row = save(rows, stations, admin, branch)
    services.set_logo(rows, row.pk, user=admin, raw=bmp())

    listed = list(rows.all())
    with django_assert_num_queries(1):              # the bytes are fetched only when touched
        _ = listed[0].logo


# -- What the tablet is told ----------------------------------------------------


def test_the_payload_carries_the_logo_so_a_station_can_print_offline(rows, stations, admin, branch, company):
    row = save(rows, stations, admin, branch)
    raw = bmp()
    services.set_logo(rows, row.pk, user=admin, raw=raw)

    payload = services.settings_payload(branch)

    assert base64.b64decode(payload["logo"]) == raw
    assert payload["order_no_prefix"] == "AZP01"
    assert payload["header_1"] == "AL ZAKHER PARK"
    assert (payload["tax_type"], payload["discount_type"]) == (company.tax_type, company.discount_type)


def test_a_tablet_whose_copy_is_current_is_not_sent_the_logo_again(rows, stations, admin, branch):
    row = save(rows, stations, admin, branch)
    services.set_logo(rows, row.pk, user=admin, raw=bmp())
    later = timezone.now() + datetime.timedelta(minutes=1)

    assert services.settings_payload(branch, since=later)["logo"] == ""
    # ... until it changes again.
    services.set_logo(rows, row.pk, user=admin, raw=bmp(height=16), now=later + datetime.timedelta(minutes=1))
    assert services.settings_payload(branch, since=later)["logo"] != ""


def test_a_station_with_no_settings_has_no_payload(branch):
    assert services.settings_payload(branch) is None


def test_deactivated_settings_stop_reaching_the_tablet(rows, stations, admin, branch):
    row = save(rows, stations, admin, branch)
    services.deactivate_settings(rows, row.pk, user=admin)

    assert services.settings_payload(branch) is None
    assert DeviceSettings.objects.filter(pk=row.pk).exists()      # the row is kept
    assert services.bill_prefix_for(branch) == branch.short_code.upper()


# -- Copy to other stations -------------------------------------------------------


def test_copying_fixes_the_wording_and_leaves_each_station_its_own(rows, stations, admin, branch, other_branch):
    source = save(rows, stations, admin, branch, footer_1="Company is not responsible for personal belongings")
    save(rows, stations, admin, other_branch, settings_code="SECOND", order_no_prefix="ABCO3",
         header_1="ABUDHABI CORNICHE 3", footer_1="Old footer", paper_feed="4")

    written, skipped = services.apply_settings_to(
        rows, source.pk, user=admin, fields=["footer_1", "paper_feed"],
        branches=[branch, other_branch],
    )

    target = DeviceSettings.objects.get(branch=other_branch)
    assert (written, skipped) == (1, 0)               # the source itself is not "skipped"
    assert (target.footer_1, target.paper_feed) == (source.footer_1, 2)
    assert (target.header_1, target.order_no_prefix) == ("ABUDHABI CORNICHE 3", "ABCO3")
    assert target.modified_by == admin


def test_a_station_with_no_settings_is_skipped_not_invented(rows, stations, admin, branch, other_branch):
    source = save(rows, stations, admin, branch)

    written, skipped = services.apply_settings_to(
        rows, source.pk, user=admin, fields=["footer_1"], branches=[other_branch],
    )

    assert (written, skipped) == (0, 1)
    assert not DeviceSettings.objects.filter(branch=other_branch).exists()


@pytest.mark.parametrize("fields,branches,message", [
    ([], True, "at least one field"),
    (["footer_1"], False, "at least one station"),
    (["settings_code"], True, "at least one field"),   # never copied
])
def test_copying_needs_something_to_copy(rows, stations, admin, branch, other_branch, fields, branches, message):
    source = save(rows, stations, admin, branch)
    with pytest.raises(services.DeviceActionError, match=message):
        services.apply_settings_to(
            rows, source.pk, user=admin, fields=fields,
            branches=[other_branch] if branches else [],
        )


# -- Endpoints and screen ---------------------------------------------------------


def post(client, url, payload=None):
    return client.post(url, json.dumps(payload or {}), content_type="application/json")


def test_the_drawer_saves_and_the_logo_uploads(signed_in, branch):
    saved = post(signed_in, "/devices/settings/save/", form(branch)).json()
    assert saved["ok"] is True

    from django.core.files.uploadedfile import SimpleUploadedFile
    raw = bmp()
    response = signed_in.post(
        f"/devices/settings/{saved['pk']}/logo/save/",
        {"logo": SimpleUploadedFile("ZAKHER.bmp", raw, content_type="image/bmp")},
    )

    assert response.json()["ok"] is True
    assert bytes(DeviceSettings.objects.with_logo().get(pk=saved["pk"]).logo) == raw

    # And the screen serves those same bytes back for its preview.
    shown = signed_in.get(f"/devices/settings/{saved['pk']}/logo/")
    assert (shown.status_code, shown["Content-Type"]) == (200, "image/bmp")
    assert shown.content == raw


def test_uploading_nothing_is_a_message_not_a_crash(signed_in, branch):
    saved = post(signed_in, "/devices/settings/save/", form(branch)).json()
    response = signed_in.post(f"/devices/settings/{saved['pk']}/logo/save/", {})

    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == "Logo"


def test_a_station_with_no_logo_has_nothing_to_serve(signed_in, branch):
    saved = post(signed_in, "/devices/settings/save/", form(branch)).json()
    assert signed_in.get(f"/devices/settings/{saved['pk']}/logo/").status_code == 404


@pytest.mark.parametrize("url", [
    "/devices/settings/save/",
    "/devices/settings/1/logo/save/",
    "/devices/settings/1/logo/clear/",
    "/devices/settings/1/apply/",
    "/devices/settings/1/deactivate/",
])
def test_every_write_needs_permission(no_permissions, url):
    assert post(no_permissions, url).status_code == 403


def test_the_screen_lists_every_station_and_says_which_have_none(signed_in, branch, other_branch):
    post(signed_in, "/devices/settings/save/", form(branch))
    html = signed_in.get("/devices/settings/").content.decode()

    rows = {
        row.split('data-branch="')[1].split('"')[0]: row
        for row in html.split("<tr ") if "data-branch=" in row
    }
    assert 'data-state="set"' in rows[str(branch.pk)]
    assert 'data-state="unset"' in rows[str(other_branch.pk)]
    assert 'data-logo="no"' in rows[str(branch.pk)]
    assert "No settings" in html
    assert "byky-device-settings.js" in html
    # The drawer's own fields, and no Active switch: settings are deactivated.
    assert 'data-field="order_no_prefix"' in html and 'data-field="copy_from"' in html
    assert 'data-field="is_active"' not in html


def test_the_screen_is_in_the_devices_sidebar_after_app_mapping(signed_in):
    html = signed_in.get("/devices/settings/").content.decode()
    order = [html.index(f'href="/devices/{p}/"') for p in ("release-mapping", "settings", "privileges")]
    assert order == sorted(order)


def test_a_system_user_may_reuse_a_code_across_companies(company, branch):
    """save_settings, not just the database constraint: a system user's
    settings_qs spans every company, so the service itself must scope the
    clash check -- the constraint alone is the last line of defence, not the
    only one (design/03-login.md section 9B.2, 20 Sep 2026)."""
    from apps.company.models import Branch, BranchType, Company, Location

    sysadmin = User.objects.create_user(
        "sys.admin", "Byky#2026", scope=UserScope.SYSTEM, allowed_channels=[Channel.WEB],
    )
    other = Company.objects.create(
        short_code="TWO", name="Second", country=company.country, state=company.state,
        phone_number="+9712222222", email="two@byky.test",
    )
    their_branch = Branch.objects.create(
        company=other,
        location=Location.objects.create(
            country=company.country, state=company.state, short_code="OTHLOC", name="Elsewhere",
        ),
        short_code="OTH01", name="Their Station", branch_type=BranchType.STATION,
    )
    every_row = scoping.settings_for(sysadmin)
    every_branch = branches_for(sysadmin)

    services.save_settings(every_row, every_branch, user=sysadmin, data=form(branch, settings_code="S01", order_no_prefix="AAA01"))
    theirs = services.save_settings(
        every_row, every_branch, user=sysadmin,
        data=form(their_branch, settings_code="S01", order_no_prefix="AAA01"),
    )

    assert theirs.company_id == other.pk
