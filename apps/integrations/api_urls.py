"""/api/v1/integrations/ -- mounted in config/urls.py before the /api/ catch-all.
Only ERPNext's own slugs match; anything else is the catch-all's JSON 404."""

import re

from django.urls import re_path

from apps.integrations import api
from apps.integrations.erpnext import SLUGS

_SLUG = "|".join(re.escape(slug) for slug in sorted(SLUGS))

urlpatterns = [
    re_path(rf"^erpnext/(?P<slug>{_SLUG})$", api.ReceiveView.as_view(), name="api-erpnext-receive"),
]
