# Generated by Django 4.2.4 on 2024-04-22 12:58

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0023_alter_economicdata_discount_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="bus",
            name="price",
            field=models.FloatField(blank=True, default=0, validators=[django.core.validators.MinValueValidator(0.0)]),
        ),
        migrations.AlterField(
            model_name="assettype",
            name="asset_type",
            field=models.CharField(
                choices=[
                    ("", "Choose..."),
                    ("dso", "dso"),
                    ("gas_dso", "gas_dso"),
                    ("h2_dso", "h2_dso"),
                    ("heat_dso", "heat_dso"),
                    ("demand", "demand"),
                    ("reducable_demand", "reducable_demand"),
                    ("gas_demand", "gas_demand"),
                    ("h2_demand", "h2_demand"),
                    ("heat_demand", "heat_demand"),
                    ("transformer_station_in", "transformer_station_in"),
                    ("transformer_station_out", "transformer_station_out"),
                    ("storage_charge_controller_in", "storage_charge_controller_in"),
                    ("storage_charge_controller_out", "storage_charge_controller_out"),
                    ("solar_inverter", "solar_inverter"),
                    ("diesel_generator", "diesel_generator"),
                    ("fuel_cell", "fuel_cell"),
                    ("gas_boiler", "gas_boiler"),
                    ("electrolyzer", "electrolyzer"),
                    ("heat_pump", "heat_pump"),
                    ("pv_plant", "pv_plant"),
                    ("wind_plant", "wind_plant"),
                    ("biogas_plant", "biogas_plant"),
                    ("geothermal_conversion", "geothermal_conversion"),
                    ("solar_thermal_plant", "solar_thermal_plant"),
                    ("charging_power", "charging_power"),
                    ("discharging_power", "discharging_power"),
                    ("capacity", "capacity"),
                    ("bess", "bess"),
                    ("gess", "gess"),
                    ("h2ess", "h2ess"),
                    ("hess", "hess"),
                ],
                max_length=30,
                unique=True,
            ),
        ),
    ]
