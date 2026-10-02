"""Offers are now Packages (2 Oct 2026): every role's grant on the Offers
screen becomes a grant on the Packages screen.

Two starting points, both handled:
- only fare.offer exists -- its row is renamed in place to fare.package, so
  every RolePermission pointing at it keeps working;
- fare.package exists already -- earlier migrations (0011, 0015-0017)
  re-apply the *current* registry, so on a database that runs them in the
  same deploy the new page appears and fare.offer is retired before this
  runs. Then each role's grant moves to the new page (ticks merged if a role
  somehow has both) and the old page goes.

Without this, re-applying the registry would leave every role without the
screen.
"""

from django.db import migrations

from apps.portal.page_sync import sync

FLAGS = ("can_create", "can_read", "can_update", "can_delete", "can_print", "can_recommend", "can_approve")


def offers_to_packages(apps, schema_editor):
    Page = apps.get_model("portal", "Page")
    RolePermission = apps.get_model("portal", "RolePermission")
    old = Page.objects.filter(code="fare.offer").first()
    new = Page.objects.filter(code="fare.package").first()
    if old is not None and new is None:
        old.code = "fare.package"
        old.save(update_fields=["code"])
    elif old is not None:
        for grant in RolePermission.objects.filter(page=old):
            existing = RolePermission.objects.filter(role_id=grant.role_id, page=new).first()
            if existing is None:
                grant.page = new
                grant.save(update_fields=["page"])
            else:
                for flag in FLAGS:
                    setattr(existing, flag, getattr(existing, flag) or getattr(grant, flag))
                existing.save(update_fields=list(FLAGS))
        old.delete()                    # takes any grant already merged into the new page
    sync(apps)


def packages_to_offers(apps, schema_editor):
    Page = apps.get_model("portal", "Page")
    if not Page.objects.filter(code="fare.offer").exists():
        Page.objects.filter(code="fare.package").update(code="fare.offer")


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0017_invoice_page'),
    ]

    operations = [
        migrations.RunPython(offers_to_packages, packages_to_offers),
    ]
