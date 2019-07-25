# Generated by Django 2.2.3 on 2019-07-26 01:21

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0011_update_proxy_permissions'),
        ('archive', '0014_auto_20190725_1801'),
    ]

    operations = [
        migrations.CreateModel(
            name='TelegramProfile',
            fields=[
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, serialize=False, to=settings.AUTH_USER_MODEL)),
                ('telegram_id', models.BigIntegerField(db_index=True, verbose_name='Telegram User ID')),
                ('photo_url', models.URLField(null=True, verbose_name='Photo URL')),
                ('username', models.CharField(max_length=128, null=True)),
                ('first_name', models.CharField(max_length=32)),
                ('last_name', models.CharField(max_length=32)),
            ],
        ),
        migrations.DeleteModel(
            name='Profile',
        ),
    ]
