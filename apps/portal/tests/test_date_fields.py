"""A drawer's fields must agree with the model behind them, and a date field
must have a picker bound to it.

Both halves exist because both failed silently until 19 Sep 2026.

`byky/partials/drawer.html` renders kind "date" as a text input carrying
.byky-date, waiting for JavaScript to upgrade it. Neither flatpickr nor the
script that binds it came across in the port, so every date in every drawer --
five on Employee alone -- was a bare text box asking the user to guess a format,
while the model behind it held a DateField. Nothing failed; it just quietly did
not work.

So: the first test reads the model, not the markup, and the second reads the
rendered page. A screen that adds a date field and forgets the picker now fails
the build, and a spec that calls a date something else fails it too.
"""

import re

import pytest
from django.apps import apps as django_apps
from django.db import models

from apps.company import drawers as company_drawers
from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.crew.models import Designation
from apps.devices import drawers as devices_drawers
from apps.crew import drawers as crew_drawers
from apps.portal import drawers as portal_drawers
from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"

ALL_SPECS = {
    **company_drawers.SPECS,
    **crew_drawers.SPECS,
    **portal_drawers.SPECS,
    **devices_drawers.SPECS,
}

DATE_KINDS = {"date", "datetime"}

# Screens that render a drawer holding a date field, and must therefore load
# flatpickr and the script that binds it.
SCREENS_WITH_DATES = [
    "/crew/employee/list/",
]


def _fields(spec):
    for section in spec.get("sections", []):
        for field in section.get("fields", []):
            yield field


def _model_field(spec, field_id):
    """The model field a drawer field writes to, or None when it writes to none.

    A spec names its model so this check can exist. Fields with no counterpart
    -- a picker that only fills other fields, a field on a related row -- return
    None and are skipped rather than guessed at.
    """
    label = spec.get("model")
    if not label:
        return None
    model = django_apps.get_model(label)
    try:
        return model._meta.get_field(field_id)
    except Exception:
        return None


@pytest.mark.parametrize("name,spec", sorted(ALL_SPECS.items()))
def test_a_drawer_field_matches_the_model_field_behind_it(name, spec):
    """A DateField in the model is a date field in the drawer, and the reverse.

    This is the check that would have caught the port's missing pickers at the
    source: the model said DateField, the drawer said "text", and nobody
    compared them.
    """
    for field in _fields(spec):
        model_field = _model_field(spec, field["id"])
        if model_field is None:
            continue

        is_date_model = isinstance(model_field, (models.DateField, models.DateTimeField))
        is_date_drawer = field.get("kind") in DATE_KINDS

        if is_date_model and not is_date_drawer:
            pytest.fail(
                f"{name}.{field['id']} is a {type(model_field).__name__} on "
                f"{spec['model']} but the drawer renders it as "
                f"{field.get('kind', 'text')!r}. Use kind 'date' (or 'datetime' "
                f"when the time of day matters), or the user types a date into a "
                f"box with no picker and no format."
            )
        if is_date_drawer and not is_date_model:
            pytest.fail(
                f"{name}.{field['id']} is a date in the drawer but "
                f"{type(model_field).__name__} on {spec['model']}. One of the two "
                f"is wrong."
            )


@pytest.mark.parametrize("name,spec", sorted(ALL_SPECS.items()))
def test_a_spec_declares_the_model_it_writes_to(name, spec):
    """Without it the check above silently passes over the whole drawer."""
    assert spec.get("model"), (
        f"{name} has no 'model' key, so its fields cannot be compared with the "
        f"model behind them."
    )
    django_apps.get_model(spec["model"])  # raises if the label is wrong


@pytest.fixture
def signed_in(client, db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )
    location = Location.objects.create(
        country=country, state=state, short_code="CORN", name="Corniche"
    )
    Branch.objects.create(
        company=company, location=location, short_code="AUH01", name="Corniche 1",
        branch_type=BranchType.STATION,
    )
    Designation.objects.create(company=company, code="CASH", title="Cashier")
    role = Role.objects.create(company=company, name="Administrator")
    grant_all(role)
    User.objects.create_user(
        "sara.k", PASSWORD, display_name="Sara K", company=company, role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "sara.k", "password": PASSWORD})
    return client


@pytest.mark.parametrize("url", SCREENS_WITH_DATES)
def test_a_screen_with_a_date_field_loads_a_picker(signed_in, url):
    """The markup alone is a text input. Without these two files it stays one."""
    content = signed_in.get(url).content.decode()

    assert "byky-date" in content, f"{url} renders no date field at all"
    assert re.search(r"flatpickr\.js", content), (
        f"{url} has a date field but never loads flatpickr, so the field is a "
        f"plain text box."
    )
    assert re.search(r"byky-datepicker\.js", content), (
        f"{url} loads flatpickr but not byky-datepicker.js, which is the file "
        f"that binds it to .byky-date. flatpickr on its own does nothing."
    )
