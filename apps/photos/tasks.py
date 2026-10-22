"""Photo jobs for the Celery worker (config/celery.py); the schedule is
CELERY_BEAT_SCHEDULE in config/settings.py."""

from celery import shared_task

from apps.photos import services


@shared_task(name="apps.photos.tasks.make_previews")
def make_previews(photo_id):
    """The display and thumbnail copies of one uploaded photo."""
    services.make_previews(photo_id)


@shared_task(name="apps.photos.tasks.make_missing_previews")
def make_missing_previews():
    """Previews the upload could not queue (the broker was down)."""
    for photo_id in services.missing_previews().values_list("pk", flat=True)[:500]:
        services.make_previews(photo_id)


@shared_task(name="apps.photos.tasks.purge_unattached")
def purge_unattached():
    """Delete the photos no request attached in time."""
    return services.purge_unattached()
