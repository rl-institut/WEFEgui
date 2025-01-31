# Generated by Django 4.2.4 on 2024-04-08 10:12

import business_model.helpers
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business_model', '0011_alter_equitydata_validators'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='equitydata',
            name='equity_developer_amount',
        ),
        migrations.AddField(
            model_name='equitydata',
            name='equity_developer_share',
            field=models.FloatField(default=0.1, help_text='Equity the mini-grid company would be able to mobilize as a percentage of total CAPEX', validators=[business_model.helpers.validate_percent], verbose_name='Mini-grid company equity (%)'),
        ),
    ]
