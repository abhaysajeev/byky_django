"""Incentive pages render, and their writes follow the rules and permissions
(design/09-incentives.md sections 7-8)."""

import datetime

import pytest
from django.urls import reverse

from apps.incentives import services
from apps.incentives.models import Run, RunStatus, Scheme
from apps.incentives.tests.test_calculate import (  # noqa: F401
    AFTER,
    NOV,
    example,
    people,
    roster,
    scheme,
)
from apps.portal.models import RolePermission
from apps.targets.tests.test_targets import local, sale, staff, two_decimals  # noqa: F401

pytestmark = pytest.mark.django_db


def post(client, name, payload, *args):
    return client.post(reverse(name, args=args), payload, content_type="application/json")


@pytest.fixture
def run(world, staff, example):  # noqa: F811
    return services.calculate(world["adc1"], NOV, staff, now=AFTER)


def test_every_page_renders(client_in, world, run, scheme):  # noqa: F811
    for url in (reverse("incentives-calculate") + "?month=2026-11", reverse("incentives-calculate"),
                reverse("incentives-run", args=[run.pk]), reverse("incentives-run", args=[run.pk]) + "?view=employee",
                reverse("incentives-employees") + "?month=2026-11", reverse("incentives-schemes"),
                reverse("incentives-scheme-add"), reverse("incentives-scheme-edit", args=[scheme.pk]),
                reverse("incentives-privileges")):
        response = client_in.get(url)
        assert response.status_code == 200, url
    page = client_in.get(reverse("incentives-run", args=[run.pk])).content.decode()
    assert "1,250.00" in page and "62.51" in page and "Approve" in page


@pytest.fixture
def december(monkeypatch):
    """The pages read today's business day themselves: make November over."""
    from apps.targets import services as targets

    monkeypatch.setattr(targets, "today_for", lambda company, now=None: datetime.date(2026, 12, 2))


def test_calculate_list_offers_the_button_only_when_allowed(client_in, world, run, december):
    page = client_in.get(reverse("incentives-calculate") + "?month=2026-11").content.decode()
    assert world["adc1"].name in page and "Recalculate" in page


def test_approve_from_the_page_then_nothing_moves(client_in, world, run, december):
    body = post(client_in, "incentives-approve", {}, run.pk).json()
    assert body["ok"], body
    run.refresh_from_db()
    assert run.status == RunStatus.APPROVED
    again = post(client_in, "incentives-calculate-post", {"branch": world["adc1"].pk, "month": "2026-11"})
    assert again.status_code == 400 and "approved" in again.json()["errors"][0]["message"]


def test_calculating_a_month_not_over_says_why(client_in, world):
    today = datetime.date.today()
    body = post(client_in, "incentives-calculate-post", {"branch": world["adc1"].pk, "month": f"{today:%Y-%m}"})
    assert body.status_code == 400 and "not over" in body.json()["errors"][0]["message"]


def test_exports_open(client_in, run):
    response = client_in.get(reverse("incentives-run-export", args=[run.pk]))
    assert response.status_code == 200 and response["Content-Type"].startswith("application/vnd.openxml")
    response = client_in.get(reverse("incentives-employees-export") + "?month=2026-11")
    assert response.status_code == 200


def test_saving_a_scheme_checks_every_formula_at_once(client_in, world):
    bad = post(client_in, "incentives-scheme-save", {
        "name": "", "effective_from": "2026-12-01", "pays_when": "achieved >= targett",
        "components": [{"name": "Labour", "applies_to": "labour", "formula": "basis * "}]}).json()
    fields = [e["field"] for e in bad["errors"]]
    assert fields == ["Name", "Pays when", "Component Labour"]
    good = post(client_in, "incentives-scheme-save", {
        "name": "December", "effective_from": "2026-12-01", "pays_when": "achieved >= target", "is_active": True,
        "variables": [{"name": "rate", "formula": "1% / max(tier2, 2)"}],
        "components": [{"name": "Primary", "applies_to": "primary", "formula": "basis * 1%"},
                       {"name": "Tier 2", "applies_to": "tier2", "formula": "basis * rate"}]}).json()
    assert good["ok"], good
    saved = Scheme.objects.get(name="December")
    assert (saved.is_active, saved.variables.count(), saved.components.count()) == (True, 1, 2)


def test_two_active_schemes_for_the_same_stations_are_refused(client_in, world, scheme):  # noqa: F811
    body = post(client_in, "incentives-scheme-save", {
        "name": "Twin", "effective_from": "2026-11-01", "pays_when": "achieved >= target", "is_active": True,
        "components": [{"name": "P", "applies_to": "primary", "formula": "1"}]}).json()
    assert not body["ok"] and "already active" in body["errors"][0]["message"]
    assert not Scheme.objects.filter(name="Twin").exists()


def test_a_scheme_used_by_an_approved_month_can_only_be_copied(client_in, world, run, scheme, staff):  # noqa: F811
    services.approve(run, staff)
    body = post(client_in, "incentives-scheme-save", {"pk": scheme.pk, "name": "x", "effective_from": "2026-11-01",
                                                      "pays_when": "1", "components": []}).json()
    assert not body["ok"] and "copy it" in body["errors"][0]["message"]
    copied = post(client_in, "incentives-scheme-copy", {}, scheme.pk).json()
    assert copied["ok"] and Scheme.objects.filter(is_active=False, name__endswith="(copy)").exists()


def test_the_live_check_and_try_it(client_in, world, example):  # noqa: F811
    assert post(client_in, "incentives-formula-check", {"formula": "basis * 1%"}).json() == {"ok": True}
    bad = post(client_in, "incentives-formula-check", {"formula": "role", "scope": "pays_when"}).json()
    assert bad["ok"] is False and "Unknown variable: role" in bad["message"]
    tried = post(client_in, "incentives-scheme-try", {
        "branch": world["adc1"].pk, "date": "2026-11-06", "pays_when": "achieved >= target",
        "components": [{"name": "Primary", "applies_to": "primary", "formula": "basis * 1%"}]}).json()
    assert tried["ok"], tried
    assert {p["name"]: p["amount"] for p in tried["people"]}["Anas"] == "12.5"


def test_writes_need_their_rights(client_in, world, run):
    RolePermission.objects.filter(role=client_in.role, page__code="incentives.calculate").update(
        can_approve=False, can_create=False)
    assert post(client_in, "incentives-approve", {}, run.pk).status_code == 403
    assert post(client_in, "incentives-calculate-post", {"branch": world["adc1"].pk, "month": "2026-11"}).status_code == 403
    assert Run.objects.get().status == RunStatus.DRAFT
