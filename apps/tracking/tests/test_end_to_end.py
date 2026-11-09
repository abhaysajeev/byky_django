"""Real reader frames through the gateway, then the engine: the whole chain."""

import datetime

import pytest
from django.utils import timezone

from apps.rental.tests import test_order_api as orders
from apps.tracking import engine
from apps.tracking import protocol as p
from apps.tracking.models import PresenceState, Reader, ReaderAssignment, VehiclePresence
from apps.tracking.tests.test_gateway_capture import DEVICE, approve
from apps.tracking.tests.test_gateway_write import read, send, upload

world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
pytestmark = pytest.mark.django_db(transaction=True)

TAG = "0A0B0C01"


def reader_clock(moment):
    """The reader's own clock: UAE time, no zone."""
    return timezone.localtime(moment).replace(tzinfo=None, microsecond=0)


def test_a_reader_upload_makes_a_bike_present_and_silence_makes_it_missing(world, shop):
    approve(company=world["company"])
    reader = Reader.objects.get(device_id=DEVICE)
    ReaderAssignment.objects.create(reader=reader, branch=world["adc1"], from_at=timezone.now())
    bike = shop["mo41"]
    bike.rfid_epc = TAG
    bike.save(update_fields=["rfid_epc"])

    first = timezone.now()
    assert send(upload(read(tag=TAG, rssi=-40, at=reader_clock(first))))[0] is not None
    engine.run_cycle()

    row = VehiclePresence.objects.get(pk=bike.pk)
    assert row.state == PresenceState.PRESENT and row.last_seen_at is not None

    # The reader keeps talking but this tag has gone: its heartbeat alone.
    later = first + datetime.timedelta(seconds=301)
    Reader.objects.filter(pk=reader.pk).update(last_heartbeat_at=later)
    engine.run_cycle(later)

    assert VehiclePresence.objects.get(pk=bike.pk).state == PresenceState.MISSING


def test_a_heartbeat_keeps_a_reader_online_through_the_gateway(world):
    approve(company=world["company"])
    reader = Reader.objects.get(device_id=DEVICE)

    assert send(p.build(p.HEARTBEAT, DEVICE, b"\x00" * 13))[0] is not None

    reader.refresh_from_db()
    assert reader.last_heartbeat_at is not None
