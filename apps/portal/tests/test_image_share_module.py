"""Client, 22 Nov 2026: Image Sharing moved from Inventory to the Rental
sidebar. The page code is unchanged, so every role's grant survives."""

from django.apps import apps as django_apps

from apps.portal.models import Page, Role, RolePermission
from apps.portal.page_sync import sync
from apps.portal.tests.test_rbac_screens import admin, company  # noqa: F401  (fixtures)


def test_image_sharing_is_under_rental_and_keeps_its_grants(admin, company):  # noqa: F811
    page = Page.objects.get(code="fleet.image_share")
    assert page.module.code == "rental"

    clerk = Role.objects.create(company=company, name="Clerk")
    RolePermission.objects.update_or_create(role=clerk, page=page, defaults={"can_read": True})
    sync(django_apps)
    assert RolePermission.objects.get(role=clerk, page__code="fleet.image_share").can_read

    assert "Image Sharing" in admin.get("/rental/privileges/").content.decode()
    assert "<span>Rental</span>" in admin.get("/fleet/image-share/list/").content.decode()
