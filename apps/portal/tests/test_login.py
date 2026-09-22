"""The sign-in rules from design/03-login.md, held in place by tests.

The order of the checks is itself a rule: nothing about an account is revealed
until the password is proved (section 4).
"""

import pytest
from django.utils import timezone

from apps.company.models import Company, Country, State
from apps.portal.auth import (
    LOCKOUT_DURATION,
    MAX_FAILED_ATTEMPTS,
    LoginRefused,
    check_account,
    check_credentials,
    close_sessions_for_user,
    sign_in_web,
)
from apps.portal.models import Role
from apps.portal.session_models import AppSession, LogoutReason
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"


@pytest.fixture
def company(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    return Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )


@pytest.fixture
def user(company):
    role = Role.objects.create(company=company, name="Administrator")
    return User.objects.create_user(
        "sara.k", PASSWORD, display_name="Sara K", company=company, role=role,
        allowed_channels=[Channel.WEB],
    )


# --- checks 1 and 2: lockout and credentials ---------------------------------

def test_an_unknown_username_and_a_wrong_password_say_the_same_thing(user):
    with pytest.raises(LoginRefused) as unknown:
        check_credentials("nobody", PASSWORD)
    with pytest.raises(LoginRefused) as wrong:
        check_credentials("sara.k", "not-the-password")

    assert unknown.value.message == wrong.value.message
    assert unknown.value.code == wrong.value.code == "invalid_credentials"


def test_username_is_matched_without_case(user):
    assert check_credentials("SARA.K", PASSWORD).pk == user.pk


def test_an_unknown_username_still_runs_a_password_check(user):
    """Regression: the identical *message* for an unknown username and a
    wrong password (tested above) does not by itself close the timing
    side-channel -- an unknown username used to return before ever hashing
    anything, while a wrong password only refused after a deliberately slow
    check_password call, making the two branches distinguishable by response
    time alone. A wall-clock assertion would be flaky in CI, so this checks
    the actual mechanism: check_credentials now calls check_password exactly
    once on the unknown-username path too (apps/portal/auth.py's
    _DUMMY_PASSWORD_HASH), not zero times."""
    from unittest.mock import patch

    from django.contrib.auth.hashers import check_password as real_check_password

    with patch("apps.portal.auth.check_password", wraps=real_check_password) as spy:
        with pytest.raises(LoginRefused):
            check_credentials("nobody-such-user", "whatever")
    spy.assert_called_once()


def test_the_account_locks_after_fifteen_wrong_passwords(user):
    for _ in range(MAX_FAILED_ATTEMPTS):
        with pytest.raises(LoginRefused):
            check_credentials("sara.k", "wrong")

    with pytest.raises(LoginRefused) as refusal:
        check_credentials("sara.k", PASSWORD)   # the right password, still refused

    assert refusal.value.code == "account_locked"


def test_the_lock_lifts_after_five_minutes(user):
    for _ in range(MAX_FAILED_ATTEMPTS):
        with pytest.raises(LoginRefused):
            check_credentials("sara.k", "wrong")

    later = timezone.now() + LOCKOUT_DURATION + timezone.timedelta(seconds=1)
    assert check_credentials("sara.k", PASSWORD, now=later).pk == user.pk


def test_a_successful_sign_in_clears_the_counter(user):
    for _ in range(3):
        with pytest.raises(LoginRefused):
            check_credentials("sara.k", "wrong")

    sign_in_web("sara.k", PASSWORD)

    user.refresh_from_db()
    assert user.failed_login_count == 0
    assert user.locked_until is None


# --- checks 3 to 6: company, user, channel, employee -------------------------

def test_an_inactive_company_refuses_everyone(user, company):
    company.is_active = False
    company.save()
    user.refresh_from_db()

    with pytest.raises(LoginRefused) as refusal:
        check_account(user, Channel.WEB)

    assert refusal.value.code == "company_inactive"


def test_an_inactive_user_is_refused(user):
    user.is_active = False

    with pytest.raises(LoginRefused) as refusal:
        check_account(user, Channel.WEB)

    assert refusal.value.code == "user_not_approved"


def test_a_user_without_the_channel_is_refused(user):
    """An operator-only account cannot sign in to the back office."""
    user.allowed_channels = [Channel.OPERATOR]

    with pytest.raises(LoginRefused) as refusal:
        check_account(user, Channel.WEB)

    assert refusal.value.code == "wrong_channel"


def test_the_order_of_the_checks_hides_account_state_from_a_stranger(user):
    """A blocked, inactive account with the wrong password still answers only
    'wrong username or password'."""
    user.is_active = False
    user.save()

    with pytest.raises(LoginRefused) as refusal:
        sign_in_web("sara.k", "not-the-password")

    assert refusal.value.code == "invalid_credentials"


# --- sessions ----------------------------------------------------------------

def test_signing_in_opens_one_session(user):
    _, session = sign_in_web("sara.k", PASSWORD, ip_address="10.0.0.9")

    assert session.is_open
    assert session.channel == Channel.WEB
    assert session.ip_address == "10.0.0.9"
    assert AppSession.objects.filter(user=user, logged_out_at__isnull=True).count() == 1


def test_a_second_web_sign_in_closes_the_first(user):
    _, first = sign_in_web("sara.k", PASSWORD)
    _, second = sign_in_web("sara.k", PASSWORD)

    first.refresh_from_db()
    assert first.logout_reason == LogoutReason.REPLACED
    assert not first.is_open
    assert second.is_open
    assert AppSession.objects.filter(user=user, logged_out_at__isnull=True).count() == 1


def test_closed_sessions_stay_as_history(user):
    sign_in_web("sara.k", PASSWORD)
    sign_in_web("sara.k", PASSWORD)

    assert AppSession.objects.filter(user=user).count() == 2


def test_blocking_closes_every_open_session(user):
    sign_in_web("sara.k", PASSWORD)

    closed = close_sessions_for_user(user, LogoutReason.EMPLOYEE_BLOCKED)

    assert closed == 1
    assert AppSession.objects.filter(user=user, logged_out_at__isnull=True).count() == 0


def test_last_login_is_recorded(user):
    sign_in_web("sara.k", PASSWORD)

    user.refresh_from_db()
    assert user.last_login is not None


# --- the web views -----------------------------------------------------------

def test_the_sign_in_page_is_public(client, db):
    assert client.get("/login/").status_code == 200


def test_the_landing_page_sends_a_stranger_to_sign_in(client, db):
    response = client.get("/")

    assert response.status_code == 302
    assert response.url == "/login/"


def test_signing_in_through_the_form_lands_on_the_dashboard(client, user):
    response = client.post("/login/", {"username": "sara.k", "password": PASSWORD})

    assert response.status_code == 302
    assert response.url == "/"
    # `/` sends a signed-in user on to the dashboard; a stranger to sign-in.
    assert client.get("/").url == "/dashboard/"


def test_a_wrong_password_re_renders_the_form_with_one_message(client, user):
    response = client.post("/login/", {"username": "sara.k", "password": "wrong"})

    assert response.status_code == 401
    assert b"Wrong username or password." in response.content


def test_signing_out_closes_the_session_and_the_cookie(client, user):
    client.post("/login/", {"username": "sara.k", "password": PASSWORD})

    client.get("/logout/")

    session = AppSession.objects.filter(user=user).first()
    assert session.logout_reason == LogoutReason.USER_LOGOUT
    assert client.get("/").url == "/login/"


def test_a_force_logout_takes_effect_on_the_web_immediately(client, user):
    """The web checks the session row on every request, so closing it elsewhere
    ends the browsing session at the next page load."""
    client.post("/login/", {"username": "sara.k", "password": PASSWORD})
    assert client.get("/").url == "/dashboard/"

    close_sessions_for_user(user, LogoutReason.FORCED)

    assert client.get("/").url == "/login/"


# --- the sign-in page ---------------------------------------------------------

def test_the_page_carries_the_form_the_view_expects(client, db):
    html = client.get("/login/").content.decode()

    assert 'name="username"' in html
    assert 'name="password"' in html
    assert "csrfmiddlewaretoken" in html
    assert "img/branding/byky-logo.png" in html
    assert "css/byky-login.css" in html


def test_a_refusal_keeps_the_username_and_drops_the_password(client, user):
    html = client.post("/login/", {"username": "sara.k", "password": "wrong"}).content.decode()

    assert 'value="sara.k"' in html
    assert "wrong" not in html.split('name="password"')[1].split(">")[0]


def test_signing_out_says_so_on_the_sign_in_page(client, user):
    """logout_view always set this message; the old page had nowhere to show it."""
    client.post("/login/", {"username": "sara.k", "password": PASSWORD})

    response = client.get("/logout/", follow=True)

    assert b"You have been signed out." in response.content


# --- where a sign-in lands ------------------------------------------------------

def test_the_page_you_were_sent_from_survives_the_form(client, user):
    """PagePermissionMixin sends a stranger to /login/?next=<page>. The form has
    to post that back, or everyone lands on the dashboard."""
    html = client.get("/login/?next=/company/branch/list/").content.decode()
    assert 'action="/login/?next=/company/branch/list/"' in html

    response = client.post(
        "/login/?next=/company/branch/list/", {"username": "sara.k", "password": PASSWORD}
    )
    assert response.url == "/company/branch/list/"


@pytest.mark.parametrize("target", [
    "https://evil.example/",
    "//evil.example/",
    "http://evil.example/company/branch/list/",
])
def test_next_never_leaves_the_site(client, user, target):
    response = client.post(f"/login/?next={target}", {"username": "sara.k", "password": PASSWORD})

    assert response.status_code == 302
    assert response.url == "/"
