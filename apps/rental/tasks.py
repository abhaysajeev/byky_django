"""Rental jobs for the Celery worker (config/celery.py)."""

import datetime

from celery import shared_task

from apps.rental import station_sync


@shared_task(name="apps.rental.tasks.station_push", ignore_result=True)
def station_push(order_id, action, device_id, item_id, at):
    """Tell the station's other tablets an order changed (station_sync)."""
    return station_sync.push(order_id, action, device_id, item_id, datetime.datetime.fromisoformat(at))


@shared_task(name="apps.rental.tasks.station_request_push", ignore_result=True)
def station_request_push(request_type, subject_id, action, status, left_out, at):
    """Tell the order's station's other tablets a request on it changed (station_sync)."""
    return station_sync.request_push(request_type, subject_id, action, status, left_out,
                                     datetime.datetime.fromisoformat(at))


@shared_task(name="apps.rental.tasks.station_test_ride_push", ignore_result=True)
def station_test_ride_push(ride_id, action, device_id, at):
    """Tell the station's other tablets a test ride changed (station_sync)."""
    return station_sync.test_ride_push(ride_id, action, device_id, datetime.datetime.fromisoformat(at))
