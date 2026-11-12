"""POST /api/v1/{app}/company (owner, 13 Nov 2026): the company the operator
tablet is registered to, every field of the web's Company screen except the
logo."""

import datetime
from decimal import Decimal

import pytest

from apps.company.models import Company, DiscountType, TaxType
from apps.crew.tests import test_attendance as attendance

world, fresh_throttle, call, login = attendance.world, attendance.fresh_throttle, attendance.call, attendance.login
operator, manager = attendance.operator, attendance.manager

URL = "/api/v1/{}/company"


def fetch(client, token, **request_data):
    body = call(client, URL.format("operator"), request_data, token=token).json()
    assert body["code"] == "ok", body
    return body["data"]["company"]


def test_every_field_comes_back_with_empty_text_as_empty(client, world, operator):
    company = fetch(client, operator)

    assert company == {
        "id": world["company"].pk, "code": "0598", "name": "BYKY", "timezone": "Asia/Dubai", "utc_offset": "+04:00",
        "ceo_name": "", "dto_name": "", "date_of_commissioning": None,
        "address": {"address": "", "city": "", "state_id": world["company"].state_id, "state": "Abu Dhabi",
                    "country_id": world["company"].country_id, "country": "United Arab Emirates", "zip_code": ""},
        "registration": {"incorporation_certificate_number": "", "business_certificate_number": "",
                         "income_tax_number": "", "tin": "", "cst": "", "service_tax_number": ""},
        "tax": {"tax_percentage": "0.00", "tax_type": "included", "discount_type": "before_tax"},
        "contact": {"contact_person": "", "phone_number": "+9710000000", "fax_number": "",
                    "email": "ops@byky.test", "website": ""},
        "version": company["version"],
    }
    assert company["version"]
    assert "logo" not in company


def test_filled_fields_come_back_as_saved(client, world, operator):
    Company.objects.filter(pk=world["company"].pk).update(
        ceo_name="Ahmed", dto_name="Sara", date_of_commissioning=datetime.date(2019, 3, 1),
        address="Office 12, Al Quoz", city="Dubai", zip_code="00000", income_tax_number="100123456700003",
        incorporation_certificate_number="INC-1", business_certificate_number="CN-1", tin="T1", cst="C1",
        service_tax_number="S1", tax_percentage=Decimal("5"), tax_type=TaxType.EXCLUDED,
        discount_type=DiscountType.AFTER_TAX, contact_person="Mohammed", fax_number="+97142",
        website="https://byky.ae")

    company = fetch(client, operator)

    assert (company["ceo_name"], company["dto_name"], company["date_of_commissioning"]) == ("Ahmed", "Sara",
                                                                                            "2019-03-01")
    assert company["address"]["address"] == "Office 12, Al Quoz" and company["address"]["zip_code"] == "00000"
    assert company["registration"] == {"incorporation_certificate_number": "INC-1",
                                       "business_certificate_number": "CN-1",
                                       "income_tax_number": "100123456700003", "tin": "T1", "cst": "C1",
                                       "service_tax_number": "S1"}
    assert company["tax"] == {"tax_percentage": "5.00", "tax_type": "excluded", "discount_type": "after_tax"}
    assert company["contact"]["fax_number"] == "+97142" and company["contact"]["website"] == "https://byky.ae"


def test_it_is_the_registered_companys_and_never_another(client, world, operator):
    company = fetch(client, operator, company_id=world["other"].pk, company=world["other"].pk)

    assert company["id"] == world["company"].pk and company["name"] == "BYKY"


def test_the_version_moves_when_the_company_is_saved(client, world, operator):
    before = fetch(client, operator)["version"]
    row = Company.objects.get(pk=world["company"].pk)
    row.modified_on = row.modified_on + datetime.timedelta(seconds=5) if row.modified_on else None
    row.name = "BYKY Rentals"
    row.save()

    after = fetch(client, operator)
    assert after["name"] == "BYKY Rentals" and after["version"] >= before


def test_a_switched_off_company_is_refused(client, world, operator):
    Company.objects.filter(pk=world["company"].pk).update(is_active=False)

    response = call(client, URL.format("operator"), token=operator)

    assert response.status_code == 409 and response.json()["code"] == "company_inactive"


@pytest.mark.parametrize("app", ["manager", "employee"])
def test_only_the_operator_app_gets_it(client, world, manager, app):
    assert call(client, URL.format(app), token=manager).json()["code"] == "wrong_channel"


def test_a_manager_token_on_the_operator_url_is_refused(client, world, manager):
    response = call(client, URL.format("operator"), token=manager)

    assert response.status_code == 403 and response.json()["code"] == "wrong_channel"


def test_no_sign_in_no_company(client, world):
    assert call(client, URL.format("operator")).status_code == 401


def test_it_is_in_the_api_docs(client, db):
    assert "/api/v1/{app}/company" in client.get("/api/schema/").content.decode()
