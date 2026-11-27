"""ERPNext > ERPNext Inbox and Privileges, and who sees the ERPNext group."""

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from apps.integrations.models import ErpnextMessage
from apps.portal.models import Page, Role, RolePermission
from apps.portal.tests.test_rbac_screens import admin, company, visitor  # noqa: F401  (fixtures)

LIST, PRIVILEGES = "/integrations/erpnext/inbox/", "/integrations/privileges/"


def message(n=1, **fields):
    values = {
        "event_id": f"evt-{n}", "slug": "branch", "doctype": "Branch", "erp_name": f"CREEK PARK {n}",
        "action": "create", "erp_modified": "2026-11-19 10:42:07", "company_name": "BYKY",
        "payload": {"event_id": f"evt-{n}", "data": {"branch": f"CREEK PARK {n}", "branch_code": f"CP{n}",
                                                     "geo": {"lat": 25.2}}},
        "received_at": timezone.now(),
    }
    values.update(fields)
    return ErpnextMessage.objects.create(**values)


def detail(m):
    return f"/integrations/erpnext/inbox/{m.pk}/"


def test_the_inbox_lists_what_erpnext_sent(admin):  # noqa: F811
    message(1)
    message(2, slug="vehicle", doctype="Serial No", erp_name="BE314", action="update",
            problems=["data is missing"])

    body = admin.get(LIST).content.decode()

    assert "CREEK PARK 1" in body and "BE314" in body and "2 messages" in body
    assert "1 problem" in body and "Update" in body


@pytest.mark.parametrize("query,shown,hidden", [
    ("doctype=Serial+No", "BE314", "CREEK PARK 1"),
    ("action=update", "BE314", "CREEK PARK 1"),
    ("problems=yes", "BE314", "CREEK PARK 1"),
    ("problems=no", "CREEK PARK 1", "BE314"),
    ("q=be31", "BE314", "CREEK PARK 1"),
])
def test_the_inbox_filters(admin, query, shown, hidden):  # noqa: F811
    message(1)
    message(2, slug="vehicle", doctype="Serial No", erp_name="BE314", action="update",
            problems=["data is missing"])

    body = admin.get(f"{LIST}?{query}").content.decode()

    assert shown in body and hidden not in body


def test_a_message_opens_with_its_fields_and_body(admin):  # noqa: F811
    m = message(1)

    body = admin.get(detail(m)).content.decode()

    assert "CREEK PARK 1" in body and "branch_code" in body and "CP1" in body
    assert "evt-1" in body and "Whole message as received" in body
    assert '{&quot;lat&quot;: 25.2}' in body                 # a nested value shown as JSON


def test_an_unreadable_message_shows_its_raw_body(admin):  # noqa: F811
    m = message(1, payload=None, raw_body="not json at all", erp_name="", problems=["body is not valid JSON"])
    body = admin.get(detail(m)).content.decode()
    assert "not json at all" in body and "body is not valid JSON" in body


def test_an_unknown_message_is_not_found(admin):  # noqa: F811
    assert admin.get("/integrations/erpnext/inbox/999999/").status_code == 404


def test_the_inbox_needs_read(visitor):  # noqa: F811
    assert visitor.get(LIST).status_code == 403
    assert visitor.get(PRIVILEGES).status_code == 403


def test_the_privilege_screen_lists_the_erpnext_pages(admin):  # noqa: F811
    body = admin.get(PRIVILEGES).content.decode()
    assert admin.get(PRIVILEGES).status_code == 200 and "ERPNext Inbox" in body


def test_the_sidebar_shows_erpnext_only_to_a_role_that_may_read_it(admin):  # noqa: F811
    assert LIST in admin.get("/dashboard/").content.decode()
    RolePermission.objects.filter(role=admin.role, page__code__startswith="integrations.").update(can_read=False)
    assert LIST not in admin.get("/dashboard/").content.decode()


def test_the_pages_are_registered_with_approve_ready_for_step_two(db):
    inbox = Page.objects.get(code="integrations.erpnext_inbox")
    assert inbox.module.code == "integrations" and inbox.actions == ["read", "approve"]
    assert Page.objects.get(code="integrations.privileges").actions == ["read", "update"]


def test_the_migration_grants_the_pages_to_privilege_admins(company):  # noqa: F811
    import importlib
    migration = importlib.import_module("apps.portal.migrations.0042_erpnext_pages")
    privileges = Page.objects.get(code="system.privileges")
    boss = Role.objects.create(company=company, name="Boss")
    clerk = Role.objects.create(company=company, name="Clerk")
    RolePermission.objects.create(role=boss, page=privileges, can_read=True, can_update=True)
    RolePermission.objects.create(role=clerk, page=privileges, can_read=True)

    migration.apply_registry(django_apps, None)

    inbox = RolePermission.objects.get(role=boss, page__code="integrations.erpnext_inbox")
    assert inbox.can_read and inbox.can_approve
    assert RolePermission.objects.get(role=boss, page__code="integrations.privileges").can_update
    assert not RolePermission.objects.filter(role=clerk, page__module__code="integrations").exists()
