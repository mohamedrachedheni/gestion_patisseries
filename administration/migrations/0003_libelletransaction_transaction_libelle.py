from django.db import migrations, models
import django.db.models.deletion


def migrate_operations_to_libelles(apps, schema_editor):
    """Regroupe les libellés existants en normalisant la casse (ex: 'bon
    livraison acompte' / 'Bon livraison acompte' -> un seul LibelleTransaction),
    exactement le doublon que ce nouveau modèle vise à empêcher."""
    Transaction = apps.get_model('administration', 'Transaction')
    LibelleTransaction = apps.get_model('administration', 'LibelleTransaction')
    db_alias = schema_editor.connection.alias

    libelles_par_cle = {}
    for transaction in Transaction.objects.using(db_alias).all():
        cle = transaction.operation.strip().lower()
        libelle = libelles_par_cle.get(cle)
        if libelle is None:
            libelle = LibelleTransaction.objects.using(db_alias).create(
                libelle=transaction.operation.strip().capitalize(),
            )
            libelles_par_cle[cle] = libelle
        transaction.libelle = libelle
        transaction.save(update_fields=['libelle'])


class Migration(migrations.Migration):

    dependencies = [
        ('administration', '0002_alter_transfere_montant'),
    ]

    operations = [
        migrations.CreateModel(
            name='LibelleTransaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('libelle', models.CharField(max_length=50, unique=True)),
                ('observation', models.CharField(blank=True, max_length=255, null=True)),
            ],
            options={
                'verbose_name': 'Libellé transaction',
                'verbose_name_plural': 'Libellés transaction',
                'ordering': ['libelle'],
            },
        ),
        migrations.AddField(
            model_name='transaction',
            name='libelle',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name='transactions',
                to='administration.libelletransaction',
            ),
        ),
        migrations.RunPython(migrate_operations_to_libelles, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='transaction',
            name='operation',
        ),
        migrations.AlterField(
            model_name='transaction',
            name='libelle',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT,
                related_name='transactions',
                to='administration.libelletransaction',
            ),
        ),
        migrations.AlterField(
            model_name='transaction',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True),
        ),
    ]
