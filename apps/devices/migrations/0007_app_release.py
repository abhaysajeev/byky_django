"""The APK release catalogue and which branches get which build.

`app_release` is dropped and recreated rather than altered. It held zero rows,
nothing referenced it, and every column changed: `version` split into
`version_name` + `version_code`, `is_minimum_supported` and `released_at` went,
the mandatory/any-time dropdown and the audit columns arrived, and a required
`company` could have had no sensible default. As alterations that was twenty-odd
operations describing a different table.

`is_minimum_supported` is not lost so much as relocated: "older versions are
refused" is now `update_type = mandatory` on whichever release the branch
resolves to, so the floor is per branch rather than global.

The composite foreign key at the end is hand-written SQL, because Django cannot
declare one. `app_release_mapping` carries copies of its release's `company` and
`channel` so that its unique index can see them; this key makes Postgres refuse
any mapping whose copies disagree with the release, and any change of a
release's channel while mappings point at it. makemigrations will never
regenerate it -- keep it if this migration is ever squashed.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.devices.models

COMPOSITE_FK = """
ALTER TABLE app_release_mapping
    ADD CONSTRAINT app_release_mapping_release_company_channel_fk
    FOREIGN KEY (release_id, company_id, channel)
    REFERENCES app_release (id, company_id, channel)
    DEFERRABLE INITIALLY DEFERRED;
"""

DROP_COMPOSITE_FK = """
ALTER TABLE app_release_mapping
    DROP CONSTRAINT IF EXISTS app_release_mapping_release_company_channel_fk;
"""

CHANNELS = [
    ("web", "Back office web"),
    ("operator", "RMS operator app"),
    ("manager", "Manager app"),
    ("employee", "Employee app"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0006_company_tax_discount_type"),
        ("core", "0005_user_employee"),
        ("devices", "0006_device_last_location"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.DeleteModel(name="AppRelease"),
        migrations.CreateModel(
            name="AppRelease",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_on", models.DateTimeField(auto_now_add=True)),
                ("modified_on", models.DateTimeField(auto_now=True, null=True)),
                ("channel", models.CharField(choices=CHANNELS, max_length=16)),
                ("version_name", models.CharField(max_length=20, validators=[apps.devices.models.VERSION_NAME])),
                ("version_code", models.PositiveIntegerField()),
                ("update_type", models.CharField(
                    choices=[("mandatory", "Mandatory"), ("anytime", "Any time")],
                    default="anytime", max_length=16,
                )),
                ("download_url", models.URLField(max_length=500)),
                ("update_note", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="app_releases", to="company.company",
                )),
                ("created_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="+", to=settings.AUTH_USER_MODEL,
                )),
                ("modified_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="+", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "db_table": "app_release",
                "ordering": ["company", "channel", "-version_code"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("company", "channel", "version_code"),
                        name="uniq_release_version_code",
                        violation_error_message="That version code is already used for this app.",
                    ),
                    models.UniqueConstraint(
                        fields=("company", "channel", "version_name"),
                        name="uniq_release_version_name",
                        violation_error_message="That version is already uploaded for this app.",
                    ),
                    models.UniqueConstraint(
                        fields=("id", "company", "channel"),
                        name="uniq_release_id_company_channel",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("version_code__gt", 0)),
                        name="app_release_version_code_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("channel", "web"), _negated=True),
                        name="app_release_channel_is_an_app",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="AppReleaseMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_on", models.DateTimeField(auto_now_add=True)),
                ("modified_on", models.DateTimeField(auto_now=True, null=True)),
                ("channel", models.CharField(choices=CHANNELS, max_length=16)),
                ("scope", models.CharField(
                    choices=[("all", "All branches"), ("branch", "One branch")], max_length=8,
                )),
                ("is_active", models.BooleanField(default=True)),
                ("branch", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="app_release_mappings", to="company.branch",
                )),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="app_release_mappings", to="company.company",
                )),
                ("created_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="+", to=settings.AUTH_USER_MODEL,
                )),
                ("modified_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="+", to=settings.AUTH_USER_MODEL,
                )),
                ("release", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="mappings", to="devices.apprelease",
                )),
            ],
            options={
                "db_table": "app_release_mapping",
                "ordering": ["company", "channel", "scope", "-created_on"],
                "indexes": [
                    models.Index(fields=["branch", "channel", "is_active"], name="app_release_branch__ae4d2d_idx"),
                    models.Index(fields=["company", "channel", "is_active"], name="app_release_company_2056e9_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("is_active", True)),
                        fields=("company", "channel", "branch"),
                        name="uniq_active_release_mapping_per_branch",
                        nulls_distinct=False,
                        violation_error_message="This branch already has an active release for this app.",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("branch__isnull", True), ("scope", "all")),
                            models.Q(("branch__isnull", False), ("scope", "branch")),
                            _connector="OR",
                        ),
                        name="app_release_mapping_scope_matches_branch",
                    ),
                ],
            },
        ),
        migrations.RunSQL(sql=COMPOSITE_FK, reverse_sql=DROP_COMPOSITE_FK),
    ]
