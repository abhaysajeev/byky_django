"""Duty Roster: which designations it staffs, and the plan itself.

design/duty roster/duty-roster.md. `Designation.roster_category` says which
job titles the roster covers (Cashier / Labour, client decision 20 Sep 2026);
`DutyRoster` is one employee's plan for one day. No data migration -- neither
table has rows yet.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0007_branch_code_per_company'),
        ('core', '0005_user_employee'),
        ('crew', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='designation',
            name='roster_category',
            field=models.CharField(blank=True, choices=[('cashier', 'Cashier'), ('labour', 'Labour')], max_length=16),
        ),
        migrations.CreateModel(
            name='DutyRoster',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_on', models.DateTimeField(auto_now_add=True)),
                ('modified_on', models.DateTimeField(auto_now=True, null=True)),
                ('date', models.DateField()),
                ('day_type', models.CharField(choices=[('working', 'Working'), ('week_off', 'Week Off'), ('sick_leave', 'Sick Leave'), ('casual_leave', 'Casual Leave')], max_length=16)),
                ('shift1_start', models.DateTimeField(blank=True, null=True)),
                ('shift1_end', models.DateTimeField(blank=True, null=True)),
                ('shift2_start', models.DateTimeField(blank=True, null=True)),
                ('shift2_end', models.DateTimeField(blank=True, null=True)),
                ('remarks', models.CharField(blank=True, max_length=200)),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='duty_roster', to='company.branch')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.user')),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='duty_roster', to='crew.employee')),
                ('modified_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.user')),
            ],
            options={
                'verbose_name_plural': 'duty roster',
                'db_table': 'duty_roster',
                'ordering': ['date', 'employee'],
                'indexes': [models.Index(fields=['employee', 'date'], name='duty_roster_employe_efb6ce_idx'), models.Index(fields=['branch', 'date'], name='duty_roster_branch__18b496_idx'), models.Index(fields=['date'], name='duty_roster_date_bf2a8c_idx')],
                'constraints': [models.UniqueConstraint(fields=('employee', 'date'), name='uniq_duty_roster_employee_date', violation_error_message='This employee already has a roster entry for that day.'), models.CheckConstraint(condition=models.Q(models.Q(('branch__isnull', False), ('day_type', 'working'), ('shift1_end__isnull', False), ('shift1_start__isnull', False)), models.Q(models.Q(('day_type', 'working'), _negated=True), ('branch__isnull', True), ('shift1_end__isnull', True), ('shift1_start__isnull', True), ('shift2_end__isnull', True), ('shift2_start__isnull', True)), _connector='OR'), name='duty_roster_working_needs_branch_and_shift1'), models.CheckConstraint(condition=models.Q(('shift1_end__gt', models.F('shift1_start'))), name='duty_roster_shift1_end_after_start'), models.CheckConstraint(condition=models.Q(models.Q(('shift2_end__isnull', True), ('shift2_start__isnull', True)), models.Q(('shift1_start__isnull', False), ('shift2_end__gt', models.F('shift2_start')), ('shift2_end__isnull', False), ('shift2_start__isnull', False)), _connector='OR'), name='duty_roster_shift2_both_or_neither_after_shift1')],
            },
        ),
    ]
