"""Cancel a settled invoice (requests.cancel_settled, owner 6 Nov 2026).

The tally test is the money proof: two orders settled the same day at the same
station -- 231 (net 110.000: 100.000 cash down, the last 10.000 by card) and
232 -- every figure read, 231's invoice cancelled, every figure read again.
Each must drop by exactly 231's money and nothing else: a cancelled order's
money counts nowhere (apps/rental/counted.py).
"""

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from apps.portal import dashboard
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.rental import credit_notes, live
from apps.rental.models import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    Order,
    OrderRequest,
    OrderRequestKind,
    OrderRequestStatus,
    OrderStatus,
    Payment,
)
from apps.rental.requests import RequestRefused, cancel_settled
from apps.rental.tests import test_manager_reports as mr
from apps.rental.tests import test_order_requests as rq
from apps.reports.tests.helpers import data, page_rows, web
from core.enums import Channel
from core.models import User, UserScope

world, token, fresh_throttle, shop, manager = rq.world, rq.token, rq.fresh_throttle, rq.shop, rq.manager
call, bill, return_all, SETTLE = rq.call, rq.bill, rq.return_all, rq.SETTLE
book, settled = mr.book, mr.settled
_ = web          # a fixture

DAY = {"from_date": "2026-10-02", "to_date": "2026-10-02"}
RECENT, DETAIL = "/api/v1/operator/invoices/recent", "/api/v1/operator/invoices/detail"
PAYMENTS = "/api/v1/operator/orders/payments"
ZONE = ZoneInfo("Asia/Dubai")
EVENING = datetime.datetime(2026, 10, 2, 22, 0, tzinfo=ZONE)
D = Decimal


@pytest.fixture
def admin(db):
    """A system administrator, as on the servers: a system-wide role with every screen."""
    role = Role.objects.create(company=None, name="System Administrator")
    grant_all(role)
    return User.objects.create_user("root.admin", "Byky#2026", display_name="Root", scope=UserScope.SYSTEM,
                                    role=role, allowed_channels=[Channel.WEB])


@pytest.fixture
def two(client, world, token, shop, settled):
    """231 settled (the `settled` fixture) and 232 settled beside it -- the same
    two vehicles again once back, the same 110.000 bill."""
    second = book(client, world, shop, token, number=232)
    return_all(client, token, second)
    assert call(client, SETTLE, bill(second, shop), token=token).json()["code"] == "ok"
    return Order.objects.get(order_no=settled["order_no"]), Order.objects.get(order_no=second["order_no"])


def _m(value):
    return D(str(value or "0").replace(",", ""))


def _last_total(d):
    """The report's closing TOTAL / GRAND TOTAL row."""
    return [r for r in d["rows"] if r["kind"] != "data"][-1]["cells"]


def figures(client, web_client, manager_token, user):
    """Every money figure, as plain Decimals / counts."""
    f = {}
    grand = _last_total(data(web_client, "rent-summary"))
    f["rent_summary"] = (grand["orders"], _m(grand["net"]), _m(grand["pay_cash"]), _m(grand["pay_card"]))

    t = _last_total(data(web_client, "rent-details"))
    f["rent_details"] = (_m(t["collected"]), _m(t["net"]), _m(t["pay_cash"]), _m(t["pay_card"]),
                         _m(t["initial"]), _m(t["total_fare"]))

    c = _last_total(data(web_client, "consolidated"))
    f["consolidated"] = (_m(c["net"]), _m(c["pay_cash"]), _m(c["pay_card"]), _m(c["actual"]))

    rows = page_rows(data(web_client, "customer-wise-rent"))
    f["customer_rent"] = sum((_m(r["cells"]["bill"]) + _m(r["cells"]["tax"]) for r in rows
                              if r["cells"]["type"] == "Invoice"), D(0))

    slide = next(s for s in dashboard.branch_live(user, now=EVENING) if s["name"] == "Abu Dhabi Corniche 1")
    f["dashboard_today"] = (slide["invoices"], _m(slide["revenue"].replace("AED", "").strip()))
    f["dashboard_month"] = D(str(sum(dashboard.monthly_sales(user, now=EVENING)["series"])))

    states = mr.get(client, manager_token, mr.BY_STATE, **DAY)
    f["manager_collections"] = (states["total"]["orders"], _m(states["total"]["net_amount"]))
    state = Order.objects.select_related("branch__location").first().branch.location.state_id
    total = mr.get(client, manager_token, mr.BY_BRANCH, state_id=state, **DAY)["total"]
    f["manager_station"] = (total["orders"], _m(total["net_amount"]), _m(total["total_card_payment"]),
                            _m(total["total_cash_payment"]))
    return f


def test_the_tally_every_figure_drops_by_exactly_the_cancelled_bill(client, web, world, token, manager, two, admin):
    first, second = two
    user = User.objects.get(username="rep.user")
    before = figures(client, web, manager, user)
    assert before["rent_summary"][0] == 2                      # both counted to start with

    cancel_settled(admin, first.pk, "Operator settled by mistake")
    after = figures(client, web, manager, user)

    net, cash, card = D("110.000"), D("100.000"), D("10.000")
    lines = sum((i.base_fare for i in first.items.all()), D(0))
    totals = sum((i.total_amount for i in first.items.all()), D(0))
    assert after["rent_summary"] == (before["rent_summary"][0] - 1, before["rent_summary"][1] - net,
                                     before["rent_summary"][2] - cash, before["rent_summary"][3] - card)
    b = before["rent_details"]
    assert after["rent_details"] == (b[0] - net, b[1] - net, b[2] - cash, b[3] - card, b[4] - lines, b[5] - totals)
    b = before["consolidated"]
    assert after["consolidated"] == (b[0] - net, b[1] - cash, b[2] - card, b[3] - net)
    assert after["customer_rent"] == before["customer_rent"] - net
    assert after["dashboard_today"] == (before["dashboard_today"][0] - 1, before["dashboard_today"][1] - net)
    assert after["dashboard_month"] == before["dashboard_month"] - net
    assert after["manager_collections"] == (before["manager_collections"][0] - 1,
                                            before["manager_collections"][1] - net)
    b = before["manager_station"]
    assert after["manager_station"] == (b[0] - 1, b[1] - net, b[2] - card, b[3] - cash)
    # and what is left is exactly order 232
    assert after["rent_summary"][1] == second.net_amount


def test_the_cancel_marks_the_invoice_void_and_keeps_the_money_on_record(two, admin):
    first, _ = two
    paid_before = list(Payment.objects.filter(order=first).values_list("pk", "amount"))

    req = cancel_settled(admin, first.pk, "  Operator settled by mistake ")

    first.refresh_from_db()
    invoice = Invoice.objects.get(order=first)
    assert first.status == OrderStatus.CANCELLED and first.cancelled_at and first.completed_at
    assert invoice.is_void and invoice.voided_by == admin and invoice.void_reason == "Operator settled by mistake"
    assert invoice.invoice_no == first.order_no                                   # number kept
    assert list(Payment.objects.filter(order=first).values_list("pk", "amount")) == paid_before
    assert (req.kind, req.status, req.decided_by, req.reason) == (
        OrderRequestKind.CANCEL, OrderRequestStatus.APPROVED, admin, "Operator settled by mistake")


@pytest.mark.parametrize("who", ["company_admin", "nobody"])
def test_only_a_system_administrator_may_cancel(two, who, world):
    first, _ = two
    user = User.objects.get(username="opr001") if who == "company_admin" else User()
    with pytest.raises(RequestRefused) as refused:
        cancel_settled(user, first.pk, "x")
    assert refused.value.status == 403


def test_a_reason_is_needed(two, admin):
    with pytest.raises(RequestRefused, match="reason"):
        cancel_settled(admin, two[0].pk, "   ")
    with pytest.raises(RequestRefused, match="at most"):
        cancel_settled(admin, two[0].pk, "x" * 256)


def test_only_a_settled_order(client, world, token, shop, admin):
    running = book(client, world, shop, token, vehicles=("mo42",), number=240)
    with pytest.raises(RequestRefused, match="settled"):
        cancel_settled(admin, Order.objects.get(order_no=running["order_no"]).pk, "x")


def test_not_twice(two, admin):
    cancel_settled(admin, two[0].pk, "x")
    with pytest.raises(RequestRefused):
        cancel_settled(admin, two[0].pk, "again")


def test_an_approved_credit_note_blocks_it(two, admin, world):
    credit_notes.issue_directly(admin, Invoice.objects.get(order=two[0]).pk, [world["company"].pk], "20")
    with pytest.raises(RequestRefused, match="credit note"):
        cancel_settled(admin, two[0].pk, "x")
    assert Order.objects.get(pk=two[0].pk).status == OrderStatus.COMPLETED


def test_after_the_cancel_nothing_more_happens_to_the_money(client, token, two, admin, world):
    first, _ = two
    invoice = Invoice.objects.get(order=first)
    cancel_settled(admin, first.pk, "x")

    # no credit note, either way
    with pytest.raises(credit_notes.CreditNoteRefused, match="cancelled"):
        credit_notes.issue_directly(admin, invoice.pk, [world["company"].pk], "10")
    assert not CreditNote.objects.filter(order=first, status=CreditNoteStatus.APPROVED).exists()
    # the tablet: not in recent invoices, not fetched for a reprint
    recent = call(client, RECENT, {}, token=token).json()["data"]
    assert invoice.invoice_no not in [i["invoice_no"] for i in recent["invoices"]]
    assert call(client, DETAIL, {"order_id": str(first.pk)}, token=token).json()["code"] == "invoice_void"


def test_live_monitoring_shows_it_cancelled_with_no_money(two, admin):
    cancel_settled(admin, two[0].pk, "x")
    order = Order.objects.prefetch_related("items__vehicle", "events__user").select_related(
        "branch__location__state", "created_by").get(pk=two[0].pk)
    r = live.row(order, ZONE)
    assert (r["last"], r["status"]) == ("Cancelled", "cancelled")
    assert all(r[k] is None for k in ("collected", "fare", "deposit", "bill", "net", "tax"))


def test_the_manager_orders_report_gives_it_no_money(client, manager, two, admin):
    cancel_settled(admin, two[0].pk, "x")
    rows = mr.get(client, manager, mr.ORDERS, **DAY)["orders"]
    row = next(r for r in rows if r["order_no"] == two[0].order_no)
    assert (row["amount"], row["advance_paid"], row["payments"], row["invoice_void"]) == (
        "0.000", "0.000", [], True)
    assert next(r for r in rows if r["order_no"] == two[1].order_no)["invoice_void"] is False


def test_the_cancelled_invoices_report_shows_what_was_handed_back(web, two, admin):
    cancel_settled(admin, two[0].pk, "Operator settled by mistake")
    today = datetime.date.today()
    d = data(web, "cancelled-invoices", **{"from": f"{today}T00:00", "to": f"{today}T23:59"})
    [row] = page_rows(d)
    c = row["cells"]
    assert (c["invoice_no"], c["net"], c["collected"], c["pay_cash"], c["pay_card"], c["reason"]) == (
        two[0].order_no, "110.000", "110.000", "100.000", "10.000", "Operator settled by mistake")


def test_the_tablet_sees_the_invoice_cancelled_in_the_order(client, token, two, admin):
    first, second = two
    cancel_settled(admin, first.pk, "Settled by mistake")
    body = call(client, "/api/v1/operator/orders/detail", {"sync_id": str(first.pk)}, token=token).json()
    assert body["code"] == "ok", body
    invoice = body["data"]["invoice"]
    assert (body["data"]["status"], invoice["status"], invoice["cancel_reason"]) == (
        "cancelled", "cancelled", "Settled by mistake")
    assert invoice["cancelled_at"] is not None
    other = call(client, "/api/v1/operator/orders/detail", {"sync_id": str(second.pk)}, token=token).json()
    assert (other["data"]["invoice"]["status"], other["data"]["invoice"]["cancelled_at"]) == ("issued", None)


def test_waiting_requests_are_closed_and_an_approved_reprint_revoked(two, admin):
    first, _ = two
    from core.ids import uuid7
    now = first.completed_at
    reprint = OrderRequest.objects.create(
        id=uuid7(), kind=OrderRequestKind.REPRINT, company_id=first.company_id, branch_id=first.branch_id,
        order=first, status=OrderRequestStatus.APPROVED, requested_at=now, decided_at=now, decided_by=admin)
    cancel_settled(admin, first.pk, "x")
    reprint.refresh_from_db()
    assert reprint.status == OrderRequestStatus.REVOKED and reprint.revoked_at is not None


def test_the_web_button_is_for_system_administrators_only(client, two, admin, web):
    first, _ = two
    page = web.get(f"/rental/order/{first.pk}/")
    assert b"Cancel invoice" not in page.content                      # a company user
    response = web.post(f"/rental/order/{first.pk}/cancel-invoice/", {"reason": "x"},
                        content_type="application/json")
    assert response.status_code == 403
    client.post("/logout/")
    client.post("/login/", {"username": "root.admin", "password": "Byky#2026"})
    assert b"Cancel invoice" in client.get(f"/rental/order/{first.pk}/").content
    ok = client.post(f"/rental/order/{first.pk}/cancel-invoice/", {"reason": "Settled by mistake"},
                     content_type="application/json").json()
    assert ok["ok"] is True
    page = client.get(f"/rental/order/{first.pk}/").content
    assert b"VOID" in page and b"Cancel invoice" not in page
    assert client.get(f"/rental/invoice/{Invoice.objects.get(order=first).pk}/receipt/").status_code == 404
