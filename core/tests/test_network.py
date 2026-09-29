"""Which address a request came from, with and without nginx in front."""

import pytest
from django.test import RequestFactory
from rest_framework.throttling import BaseThrottle

from core.network import client_ip

NGINX = "172.18.0.5"          # the proxy container, as gunicorn sees it
VISITOR = "203.0.113.7"


def request(remote_addr=NGINX, forwarded=None):
    extra = {"REMOTE_ADDR": remote_addr}
    if forwarded is not None:
        extra["HTTP_X_FORWARDED_FOR"] = forwarded
    return RequestFactory().get("/", **extra)


@pytest.fixture
def behind_nginx(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}


@pytest.fixture
def no_proxy(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 0}


def test_without_a_proxy_the_header_is_ignored(no_proxy):
    """Nobody vouches for X-Forwarded-For here: a caller could write anything."""
    assert client_ip(request(remote_addr=VISITOR, forwarded="10.9.9.9")) == VISITOR


def test_behind_nginx_the_visitor_is_the_forwarded_address(behind_nginx):
    assert client_ip(request(forwarded=VISITOR)) == VISITOR


def test_behind_nginx_a_forged_entry_on_the_left_is_ignored(behind_nginx):
    """Should the proxy ever append instead of overwrite, only its own entry counts."""
    assert client_ip(request(forwarded=f"10.9.9.9, {VISITOR}")) == VISITOR


def test_behind_nginx_a_call_without_the_header_falls_back_to_the_connection(behind_nginx):
    """The container health check talks to gunicorn directly, not through nginx."""
    assert client_ip(request(remote_addr="127.0.0.1")) == "127.0.0.1"


def test_ipv6_is_kept(behind_nginx):
    assert client_ip(request(forwarded="2001:db8::1")) == "2001:db8::1"


@pytest.mark.parametrize("forwarded", ["not-an-ip", " , ", "203.0.113.7:5000"])
def test_a_malformed_address_is_none_not_a_failed_save(behind_nginx, forwarded):
    """The IP columns are inet: storing garbage would fail the whole write."""
    assert client_ip(request(forwarded=forwarded)) is None


def test_an_empty_header_falls_back_to_the_connection(behind_nginx):
    assert client_ip(request(remote_addr="127.0.0.1", forwarded="")) == "127.0.0.1"


@pytest.mark.parametrize("num_proxies", [0, 1, 2])
@pytest.mark.parametrize("forwarded", [None, VISITOR, f"10.9.9.9, {VISITOR}", f"10.9.9.9, 10.8.8.8, {VISITOR}"])
def test_the_logs_and_the_throttles_name_the_same_caller(settings, num_proxies, forwarded):
    """A throttled device must show up in the request log under the same address."""
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": num_proxies}
    req = request(forwarded=forwarded)
    assert client_ip(req) == BaseThrottle().get_ident(req)
