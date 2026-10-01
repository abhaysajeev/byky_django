import datetime
import json
import uuid

import pytest
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.devices.models import Device, DeviceStatus
from apps.discount.models import (
    CardDiscount,
    CardDiscountClaim,
    CardDiscountDay,
    CardGrade,
    CardType,
    ClaimStatus,
    FareBasis,
    UsageType,
)
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.rental.models import Customer, Order
from core.enums import Channel, UserScope
from core.models import User

PASSWORD = "Byky#2026"


@pytest.fixture
def world(db):
    uae = Country.objects.create(short_code="AE", name="United Arab Emirates")
    abu_dhabi = State.objects.create(country=uae, short_code="AUH", name="Abu Dhabi")
    location = Location.objects.create(country=uae, state=abu_dhabi, short_code="CORN", name="Corniche")

    def company(code, name, phone):
        return Company.objects.create(short_code=code, name=name, country=uae, state=abu_dhabi,
                                      phone_number=phone, email=f"ops@{code.lower()}.test")

    ours, theirs = company("BYKY", "BYKY", "+9710000000"), company("OTHER", "Other Co", "+9711111111")

    def refs(co, tag):
        card_type = CardType.objects.create(company=co, code=f"T{tag}", name=f"{tag} Card")
        return {
            "type": card_type,
            "grade": CardGrade.objects.create(company=co, card_type=card_type, code=f"G{tag}", name=f"{tag} Gold"),
            "branch": Branch.objects.create(company=co, location=location, short_code=f"S{tag}",
                                            name=f"{tag} Station", branch_type=BranchType.STATION),
            "customer": Customer.objects.create(company=co, customer_code="CU001", first_name=f"{tag} Anil",
                                                mobile_country_code="+971",
                                                mobile_no="5011111" + ("11" if tag == "Ours" else "22")),
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
    return sign_in(client, "sara.k", company=world["ours"])


@pytest.fixture
def system_client(client, world):
    return sign_in(client, "platform.admin", scope=UserScope.SYSTEM)


def post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def make_discount(grade, *, start=datetime.date(2026, 10, 1), end=datetime.date(2026, 12, 31), percent="10",
                  requires_approval=False, is_active=True):
    discount = CardDiscount.objects.create(
        company=grade.company, card_grade=grade, valid_from=start, valid_to=end,
        requires_approval=requires_approval, fare_basis=FareBasis.BASE,
        usage_type=UsageType.PER_DAY, usage_limit=2, is_active=is_active,
    )
    for day in range(7):
        CardDiscountDay.objects.create(card_discount=discount, weekday=day, discount_percent=percent)
    return discount


def make_order(customer, *, branch=None, order_no="", **fields):
    """A rental order for a claim to point at -- the order's own details are
    not what these tests are about."""
    company = customer.company
    branch = branch or Branch.objects.filter(company=company).first()
    device = Device.objects.get_or_create(
        installation_id=f"till-{company.short_code}",
        defaults={"company": company, "platform": "android", "channel": Channel.OPERATOR,
                  "status": DeviceStatus.APPROVED, "approved_at": timezone.now()},
    )[0]
    values = {
        "id": uuid.uuid4(), "company": company, "branch": branch, "device": device, "customer": customer,
        "customer_name": customer.full_name, "customer_mobile": customer.mobile_full,
        "order_no": order_no or f"ORD-{uuid.uuid4().hex[:6]}", "booked_at": timezone.now(),
        "start_time": timezone.now(), "total_amount": "100.00", "net_amount": "100.00",
    }
    values.update(fields)
    return Order.objects.create(**values)


def make_claim(discount, customer, *, status=ClaimStatus.PENDING, when=None, order=None, **fields):
    when = when or timezone.now()
    order = order or make_order(customer)
    decided = status in (ClaimStatus.APPROVED, ClaimStatus.REJECTED) or (
        status == ClaimStatus.REDEEMED and discount.requires_approval)
    values = {
        "id": uuid.uuid4(), "company": discount.company, "card_discount": discount,
        "card_type": discount.card_grade.card_type, "card_grade": discount.card_grade,
        "discount_percent": "10", "fare_basis": discount.fare_basis, "requires_approval": discount.requires_approval,
        "customer": customer, "customer_name": customer.full_name, "mobile_full": customer.mobile_full
        if hasattr(customer, "mobile_full") else customer.mobile_no,
        "order": order, "status": status, "requested_at": when,
        "decided_at": when if decided or status == ClaimStatus.CANCELLED else None,
        "redeemed_at": when if status == ClaimStatus.REDEEMED else None,
    }
    values.update(fields)
    return CardDiscountClaim.objects.create(**values)
