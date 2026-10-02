"""Offers are now Packages (2 Oct 2026). Everything is renamed in place --
models, tables, fields, constraints, the stored "offer_price" choice --
so every saved row, and every order line pointing at one, is kept.

Constraints are renamed with ALTER TABLE ... RENAME CONSTRAINT: the database
never drops or re-checks them. Django's own record of them is updated beside
that (SeparateDatabaseAndState): the old names come off its state before the
fields they name are renamed, the new names go on at the end.
"""

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F, Q

RENAMED_CONSTRAINTS = [  # table, old name, new name
    ("package", "uniq_offer_code_per_company", "uniq_package_code_per_company"),
    ("package", "offer_valid_dates", "package_valid_dates"),
    ("package", "offer_value_band", "package_value_band"),
    ("package", "offer_scope_matches_level", "package_scope_matches_level"),
    ("package_item", "offer_item_package_range", "package_item_package_range"),
    ("package_item", "offer_item_value_not_negative", "package_item_value_not_negative"),
    ("package_free_item", "offer_free_item_package_range", "package_free_item_package_range"),
    ("package_free_item", "offer_free_item_value_not_negative", "package_free_item_value_not_negative"),
    ("package_free_item_time_slab", "offer_slab_time_order", "package_slab_time_order"),
    ("package_free_item_time_slab", "offer_slab_date_matches_mode", "package_slab_date_matches_mode"),
    ("package_free_item_time_slab", "offer_slab_package_range", "package_slab_package_range"),
    ("package_free_item_time_slab", "offer_slab_value_not_negative", "package_slab_value_not_negative"),
]
MODEL_OF = {"package": "package", "package_item": "packageitem", "package_free_item": "packagefreeitem",
            "package_free_item_time_slab": "packagefreeitemtimeslab"}
MAX_MINUTES = 1440

NEW_CONSTRAINTS = {
    "package": [
        models.UniqueConstraint(fields=("company", "package_code"), name="uniq_package_code_per_company"),
        models.CheckConstraint(condition=Q(valid_to__gte=F("valid_from")), name="package_valid_dates"),
        models.CheckConstraint(condition=Q(upper_value__gte=F("lower_value")), name="package_value_band"),
        models.CheckConstraint(
            condition=Q(level="company", branch__isnull=True, location__isnull=True)
            | Q(level="branch", branch__isnull=False, location__isnull=True)
            | Q(level="location", branch__isnull=True, location__isnull=False),
            name="package_scope_matches_level",
        ),
    ],
    "packageitem": [
        models.CheckConstraint(condition=Q(package_minutes__gt=0, package_minutes__lte=MAX_MINUTES),
                               name="package_item_package_range"),
        models.CheckConstraint(condition=Q(value__isnull=True) | Q(value__gte=0),
                               name="package_item_value_not_negative"),
    ],
    "packagefreeitem": [
        models.CheckConstraint(condition=Q(package_minutes__gt=0, package_minutes__lte=MAX_MINUTES),
                               name="package_free_item_package_range"),
        models.CheckConstraint(condition=Q(value__gte=0), name="package_free_item_value_not_negative"),
    ],
    "packagefreeitemtimeslab": [
        models.CheckConstraint(condition=Q(to_time__gt=F("from_time")), name="package_slab_time_order"),
        models.CheckConstraint(
            condition=Q(date_mode="all_dates", specific_date__isnull=True)
            | Q(date_mode="specific_date", specific_date__isnull=False),
            name="package_slab_date_matches_mode",
        ),
        models.CheckConstraint(condition=Q(package_minutes__gt=0, package_minutes__lte=MAX_MINUTES),
                               name="package_slab_package_range"),
        models.CheckConstraint(condition=Q(value__gte=0), name="package_slab_value_not_negative"),
    ],
}

TIDY_GENERATED_NAMES = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.conrelid::regclass AS tbl, c.conname FROM pg_constraint c
           WHERE c.conrelid IN ('package'::regclass, 'package_item'::regclass, 'package_free_item'::regclass,
                                'package_free_item_time_slab'::regclass)
             AND c.conname LIKE 'offer%'
  LOOP
    EXECUTE format('ALTER TABLE %s RENAME CONSTRAINT %I TO %I', r.tbl, r.conname,
                   'package' || substr(r.conname, 6));
  END LOOP;
  FOR r IN SELECT i.relname FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
           WHERE x.indrelid IN ('package'::regclass, 'package_item'::regclass, 'package_free_item'::regclass,
                                'package_free_item_time_slab'::regclass)
             AND i.relname LIKE 'offer%'
  LOOP
    EXECUTE format('ALTER INDEX %I RENAME TO %I', r.relname, 'package' || substr(r.relname, 6));
  END LOOP;
  FOR r IN SELECT relname FROM pg_class WHERE relkind = 'S' AND relname LIKE 'offer%id_seq'
  LOOP
    EXECUTE format('ALTER SEQUENCE %I RENAME TO %I', r.relname, 'package' || substr(r.relname, 6));
  END LOOP;
END $$;
"""

PROMOTION_TYPES = [("quantity", "Quantity"), ("amount", "Amount"), ("percentage", "Percentage"),
                   ("each", "Each"), ("package_price", "Package Price")]
FREE_OR_PRICE = [("free", "Free"), ("package_price", "Package Price")]


def offer_price_to_package_price(apps, schema_editor):
    Package = apps.get_model("fare", "Package")
    Package.objects.filter(promotion_type="offer_price").update(promotion_type="package_price")
    Package.objects.filter(free_or_package_price="offer_price").update(free_or_package_price="package_price")


def package_price_to_offer_price(apps, schema_editor):
    Package = apps.get_model("fare", "Package")
    Package.objects.filter(promotion_type="package_price").update(promotion_type="offer_price")
    Package.objects.filter(free_or_package_price="package_price").update(free_or_package_price="offer_price")


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0001_initial'),
        ('fleet', '0001_initial'),
        ('fare', '0003_offer_offerfreeitem_offerfreeitemtimeslab_offeritem_and_more'),
        # After every rental migration that still names the model fare.offer
        # (OrderItem.offer), so the rename below carries that link with it on
        # any database, whatever order the two apps would otherwise run in.
        ('rental', '0010_item_removed'),
    ]

    operations = [
        # Django forgets the old constraint names first: two of them name fields renamed below.
        migrations.SeparateDatabaseAndState(state_operations=[
            migrations.RemoveConstraint(model_name=model, name=old)
            for model, old in [
                ("offer", "uniq_offer_code_per_company"), ("offer", "offer_valid_dates"),
                ("offer", "offer_value_band"), ("offer", "offer_scope_matches_level"),
                ("offeritem", "offer_item_package_range"), ("offeritem", "offer_item_value_not_negative"),
                ("offerfreeitem", "offer_free_item_package_range"),
                ("offerfreeitem", "offer_free_item_value_not_negative"),
                ("offerfreeitemtimeslab", "offer_slab_time_order"),
                ("offerfreeitemtimeslab", "offer_slab_date_matches_mode"),
                ("offerfreeitemtimeslab", "offer_slab_package_range"),
                ("offerfreeitemtimeslab", "offer_slab_value_not_negative"),
            ]
        ]),

        migrations.RenameModel(old_name='Offer', new_name='Package'),
        migrations.RenameModel(old_name='OfferItem', new_name='PackageItem'),
        migrations.RenameModel(old_name='OfferFreeItem', new_name='PackageFreeItem'),
        migrations.RenameModel(old_name='OfferFreeItemTimeSlab', new_name='PackageFreeItemTimeSlab'),
        migrations.AlterModelTable(name='package', table='package'),
        migrations.AlterModelTable(name='packageitem', table='package_item'),
        migrations.AlterModelTable(name='packagefreeitem', table='package_free_item'),
        migrations.AlterModelTable(name='packagefreeitemtimeslab', table='package_free_item_time_slab'),

        migrations.RenameField(model_name='package', old_name='offer_code', new_name='package_code'),
        migrations.RenameField(model_name='package', old_name='offer_name', new_name='package_name'),
        # Straight after the rename: the default ordering names the old column.
        migrations.AlterModelOptions(name='package', options={'ordering': ['-valid_from', 'package_name']}),
        migrations.RenameField(model_name='package', old_name='free_or_offer_price', new_name='free_or_package_price'),
        migrations.RenameField(model_name='packageitem', old_name='offer', new_name='package'),
        migrations.RenameField(model_name='packagefreeitem', old_name='offer', new_name='package'),
        migrations.RenameField(model_name='packagefreeitemtimeslab', old_name='offer', new_name='package'),

        # Wider first: "package_price" is 13 characters, the columns held 12.
        migrations.AlterField(
            model_name='package', name='promotion_type',
            field=models.CharField(choices=PROMOTION_TYPES, max_length=15, verbose_name='Promotion Type'),
        ),
        migrations.AlterField(
            model_name='package', name='free_or_package_price',
            field=models.CharField(choices=FREE_OR_PRICE, default='free', max_length=15,
                                   verbose_name='Free / Package Price'),
        ),
        migrations.RunPython(offer_price_to_package_price, package_price_to_offer_price),
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),

        migrations.AlterField(
            model_name='package', name='package_code', field=models.CharField(max_length=30, verbose_name='Package Code'),
        ),
        migrations.AlterField(
            model_name='package', name='package_name', field=models.CharField(max_length=100, verbose_name='Package Name'),
        ),
        migrations.AlterField(
            model_name='package', name='level',
            field=models.CharField(
                choices=[('company', 'Company Wise'), ('branch', 'Branch Wise'), ('location', 'Location Wise')],
                default='company', max_length=10, verbose_name='Package Level',
            ),
        ),
        migrations.AlterField(
            model_name='package', name='company',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='packages',
                                    to='company.company'),
        ),
        migrations.AlterField(
            model_name='package', name='branch',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name='packages', to='company.branch'),
        ),
        migrations.AlterField(
            model_name='package', name='location',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name='packages', to='company.location'),
        ),
        migrations.AlterField(
            model_name='packageitem', name='vehicle_type',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='package_items',
                                    to='fleet.vehicletype'),
        ),
        migrations.AlterField(
            model_name='packagefreeitem', name='vehicle_type',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='package_free_items',
                                    to='fleet.vehicletype'),
        ),
        migrations.AlterField(
            model_name='packagefreeitemtimeslab', name='vehicle_type',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='package_time_slabs',
                                    to='fleet.vehicletype'),
        ),

        # The constraints, renamed in the database and re-recorded under the new names.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"',
                    f'ALTER TABLE "{table}" RENAME CONSTRAINT "{new}" TO "{old}"',
                )
                for table, old, new in RENAMED_CONSTRAINTS
            ],
            state_operations=[
                migrations.AddConstraint(model_name=model, constraint=constraint)
                for model, constraints in NEW_CONSTRAINTS.items()
                for constraint in constraints
            ],
        ),

        # Postgres's own generated names (primary keys, foreign keys, the
        # positive-number checks, their indexes, the id sequences) still start
        # with "offer". Renamed too, so a migrated database matches a fresh one.
        # Cosmetic -- nothing refers to them by name -- so going back leaves them.
        migrations.RunSQL(TIDY_GENERATED_NAMES, migrations.RunSQL.noop),
    ]
