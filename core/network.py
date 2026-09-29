"""The address a request really came from.

On the VPS the app sits behind nginx (deploy/nginx/qa.conf), so REMOTE_ADDR is
nginx's own container address for every visitor. nginx passes the visitor's
address in X-Forwarded-For, but that header can also be sent by the visitor
themselves -- so it is trusted only as far as REST_FRAMEWORK's NUM_PROXIES says
proxies exist, by the same rule DRF's throttles use to pick a bucket
(BaseThrottle.get_ident). The logs, the session audit and the throttles
therefore always agree on who a caller is.
"""

import ipaddress

from rest_framework.settings import api_settings


def client_ip(request):
    """The caller's IP address, or None when there is no usable one.

    With NUM_PROXIES = 0 (no proxy) X-Forwarded-For is ignored entirely. With
    N proxies the address is the Nth entry from the right: each trusted proxy
    appends one, so anything further left was written by the caller.
    """
    remote_addr = request.META.get("REMOTE_ADDR")
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    num_proxies = api_settings.NUM_PROXIES or 0
    if num_proxies and forwarded:
        addresses = forwarded.split(",")
        candidate = addresses[-min(num_proxies, len(addresses))].strip()
    else:
        candidate = remote_addr
    # The IP columns are Postgres inet: a malformed value would fail the save.
    try:
        return str(ipaddress.ip_address((candidate or "").strip()))
    except ValueError:
        return None
