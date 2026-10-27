"""Rental jobs for the Celery worker (config/celery.py)."""

import datetime

from celery import shared_task

from apps.rental import station_sync


@shared_task(name="apps.rental.tasks.station_push", ignore_result=True)
def station_push(order_id, action, device_id, item_id, at):
    """Tell the station's other tablets an order changed (station_sync)."""
    return station_sync.push(order_id, action, device_id, item_id, datetime.datetime.fromisoformat(at))
