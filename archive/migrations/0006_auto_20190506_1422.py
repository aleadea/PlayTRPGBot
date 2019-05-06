# Generated by Django 2.2.1 on 2019-05-06 14:22

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('archive', '0005_chat_password'),
    ]

    operations = [
        migrations.AlterField(
            model_name='log',
            name='content',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.CreateModel(
            name='Tag',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=128)),
                ('chat', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='archive.Chat')),
            ],
        ),
        migrations.AddField(
            model_name='log',
            name='tag',
            field=models.ManyToManyField(to='archive.Tag'),
        ),
    ]
