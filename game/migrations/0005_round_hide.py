# Generated by Django 2.2.1 on 2019-05-20 22:30

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('game', '0004_player'),
    ]

    operations = [
        migrations.AddField(
            model_name='round',
            name='hide',
            field=models.BooleanField(default=False),
        ),
    ]
