# Generated by Django 4.2.4 on 2023-12-15 16:30

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("business_model", "0005_alter_businessmodel_model_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="bmquestion",
            name="sub_question_to",
            field=models.IntegerField(null=True),
        ),
    ]
