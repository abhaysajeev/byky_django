"""Tokens ERPNext uses to post here (RMS Settings -> Auth Token in byky_app).

    manage.py erpnext_token create <label>    # prints the token ONCE
    manage.py erpnext_token list
    manage.py erpnext_token revoke <label>

Only the token's SHA-256 is stored. A lost token cannot be shown again --
revoke it and create a new one. One token per environment that posts here.
"""

import secrets

from django.core.management.base import BaseCommand, CommandError

from apps.integrations.models import ErpnextToken
from apps.integrations.services import hash_token


class Command(BaseCommand):
    help = "Create, list or revoke the bearer tokens ERPNext posts with."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["create", "list", "revoke"])
        parser.add_argument("label", nargs="?", help="a name for the token, e.g. erpnext-test")

    def handle(self, *args, action, label=None, **options):
        if action == "list":
            for token in ErpnextToken.objects.order_by("label"):
                used = token.last_used_at.isoformat(timespec="seconds") if token.last_used_at else "never"
                state = "active" if token.is_active else "revoked"
                self.stdout.write(f"{token.label}\t{state}\tcreated {token.created_at:%Y-%m-%d}\tlast used {used}")
            return
        if not label:
            raise CommandError(f"{action} needs a label.")
        if action == "create":
            if ErpnextToken.objects.filter(label=label).exists():
                raise CommandError(f"A token labelled {label!r} already exists. Revoke it or pick another label.")
            token = secrets.token_urlsafe(32)
            ErpnextToken.objects.create(label=label, token_hash=hash_token(token))
            self.stdout.write(self.style.SUCCESS(f"Token {label!r} created. It is shown only now:"))
            self.stdout.write(token)
            return
        updated = ErpnextToken.objects.filter(label=label, is_active=True).update(is_active=False)
        if not updated:
            raise CommandError(f"No active token labelled {label!r}.")
        self.stdout.write(self.style.SUCCESS(f"Token {label!r} revoked."))
