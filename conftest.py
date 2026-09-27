"""Test-wide settings tweaks."""

import pytest


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Serve static files by name in tests.

    Production uses WhiteNoise's manifest storage, which rewrites every
    {% static %} path to a hashed name and raises if the file is not in a
    manifest built by collectstatic. Tests render templates without that build
    step, so they use the plain backend; `manage.py collectstatic` is what
    proves the real paths exist.
    """
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }


@pytest.fixture(autouse=True)
def fast_password_hashing(settings):
    """Hash passwords cheaply in tests.

    The real hasher is deliberately slow, which is right in production and
    pointless here -- the lockout test alone makes 15 attempts. This is the
    standard Django testing trade-off: security of the stored hash is not what
    these tests are checking.
    """
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture(autouse=True)
def inline_log_writes(settings):
    """Write request and error logs inline, inside the test's own transaction.

    In a running server a background thread writes them in batches
    (apps/monitoring/writer.py); a test that makes a call and then reads its log
    row needs the row there at once, and rolled back with everything else.
    """
    settings.MONITORING_SYNC = True


@pytest.fixture
def logs_off(monkeypatch):
    """No request/error log writes, for a test that counts the queries a call
    itself makes. In a running server the log row is written by a background
    thread outside the request, so it is not part of what such a test measures."""
    from apps.monitoring import writer

    monkeypatch.setattr(writer, "enqueue", lambda kind, fields: None)
