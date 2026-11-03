"""The order display status (apps/rental/order_status.py, CR5 3 Nov 2026): the
eight values, each in SQL and in Python, kept in step."""

import pytest
from django.utils import timezone

from apps.rental import order_status as ds
from apps.rental.models import Order, OrderItem, OrderItemStatus, OrderStatus
from apps.rental.tests import test_order_requests as rq
from apps.rental.tests.test_manager_reports import book

world, token, fresh_throttle, shop, manager = rq.world, rq.token, rq.fresh_throttle, rq.shop, rq.manager


@pytest.fixture
def order(client, world, token, shop):
    data = book(client, world, shop, token, vehicles=("mo41", "dc02", "mo42"))
    return Order.objects.get(order_no=data["order_no"])


def lines(order):
    return list(order.items.order_by("start_time", "created_on", "pk"))


def set_line(item, status=None, parked=False):
    fields = {"return_pending_at": timezone.now() if parked else None}
    if status:
        fields["status"] = status
    OrderItem.objects.filter(pk=item.pk).update(**fields)


def shown(order):
    """The status in SQL and in Python -- they must agree."""
    in_sql = ds.with_display_status(Order.objects.filter(pk=order.pk)).get().display_status
    fresh = Order.objects.get(pk=order.pk)
    in_python = ds.display_status(fresh)
    assert in_sql == in_python, (in_sql, in_python)
    return in_sql


def test_running_then_partially_received_then_awaiting(order):
    a, b, c = lines(order)
    assert shown(order) == ds.RUNNING
    set_line(a, OrderItemStatus.RETURNED)
    assert shown(order) == ds.PARTIALLY_RECEIVED
    set_line(b, OrderItemStatus.RETURNED)
    set_line(c, OrderItemStatus.RETURNED)
    assert shown(order) == ds.AWAITING_SETTLEMENT


def test_a_swap_stays_visible_at_every_stage(order):
    a, b, c = lines(order)
    set_line(a, OrderItemStatus.REPLACED)
    assert shown(order) == ds.REPLACED
    set_line(b, OrderItemStatus.RETURNED)
    assert shown(order) == ds.REPLACED_PARTIALLY_RECEIVED
    set_line(c, OrderItemStatus.RETURNED)
    assert shown(order) == ds.AWAITING_SETTLEMENT
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.COMPLETED)
    assert shown(order) == ds.REPLACED_RECEIVED


def test_settled_without_a_swap_is_received(order):
    for item in lines(order):
        set_line(item, OrderItemStatus.RETURNED)
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.COMPLETED)
    assert shown(order) == ds.RECEIVED


def test_cancelled_wins_even_after_returns(order):
    set_line(lines(order)[0], OrderItemStatus.RETURNED)
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.CANCELLED)
    assert shown(order) == ds.CANCELLED


def test_a_parked_return_counts_as_back(order):
    a, b, c = lines(order)
    set_line(a, parked=True)
    assert shown(order) == ds.PARTIALLY_RECEIVED
    set_line(b, parked=True)
    set_line(c, parked=True)
    assert shown(order) == ds.AWAITING_SETTLEMENT


def test_a_removed_line_is_neither_out_nor_back(order):
    a, b, c = lines(order)
    set_line(a, OrderItemStatus.REMOVED)
    assert shown(order) == ds.RUNNING
    set_line(b, OrderItemStatus.RETURNED)
    set_line(c, OrderItemStatus.RETURNED)
    assert shown(order) == ds.AWAITING_SETTLEMENT


def test_every_value_has_a_label_badge_and_tone():
    assert len(ds.VALUES) == 8
    for value in ds.VALUES:
        assert ds.label(value) and value in ds.BADGES and value in ds.TONES
