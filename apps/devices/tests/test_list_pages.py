"""Client UI round, 20 Nov 2026: the Export buttons that were placeholders now
export (.xlsx, byky-screen.js data-scr-export); long lists page at 50; Device
Approval shows the Platform ID as Device Mapping does."""

import pytest
from django.urls import reverse

from apps.devices.tests.test_screens import approved_device  # noqa: F401
from apps.portal.models import RolePermission

pytestmark = pytest.mark.django_db

EXPORT_PAGES = [
    "company-company-list", "company-branch-list", "company-department-list", "company-location-list",
    "company-country-state-list", "rental-payment-mode-list", "fleet-brand-list", "fleet-category-list",
    "fleet-vehicle-type-list", "fleet-uom-list", "fleet-asset-type-list", "fleet-issue-type-list",
    "fleet-asset-list", "devices-device-approval", "devices-device-mapping",
]


@pytest.mark.parametrize("name", EXPORT_PAGES)
def test_every_former_placeholder_export_now_exports(signed_in, name):
    body = signed_in.get(reverse(name)).content.decode()
    assert "data-scr-export=" in body and "vendor/libs/xlsx/xlsx.full.min.js" in body, name


def test_export_needs_the_print_permission(signed_in):
    RolePermission.objects.filter(page__code="fleet.brand").update(can_print=False)
    assert "data-scr-export=" not in signed_in.get(reverse("fleet-brand-list")).content.decode()


def test_device_approval_shows_the_platform_id_and_pages_at_50(signed_in, approved_device):  # noqa: F811
    body = signed_in.get(reverse("devices-device-approval")).content.decode()
    assert body.count("<th>Platform ID</th>") == 3                       # every tab
    assert '<span class="scr-code">fed25f408ca56786</span>' in body
    assert "fed25f408ca56786" in body[body.index('data-state="approved"') - 400:]   # searchable on its row
    assert body.count('data-scr-page-size="50"') == 3


def test_the_mapping_pages_page_at_50(signed_in, approved_device):  # noqa: F811
    for name in ("devices-device-mapping", "devices-app-mapping"):
        assert 'data-scr-page-size="50"' in signed_in.get(reverse(name)).content.decode(), name
