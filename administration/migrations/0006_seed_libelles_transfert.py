from django.db import migrations


LIBELLE_TRANSFERT_RECU = 'Transfère montant reçu'
LIBELLE_TRANSFERT_VERSE = 'Transfère montant versé'


def seed_libelles(apps, schema_editor):
    LibelleTransaction = apps.get_model('administration', 'LibelleTransaction')
    db_alias = schema_editor.connection.alias
    LibelleTransaction.objects.using(db_alias).get_or_create(libelle=LIBELLE_TRANSFERT_RECU)
    LibelleTransaction.objects.using(db_alias).get_or_create(libelle=LIBELLE_TRANSFERT_VERSE)


def unseed_libelles(apps, schema_editor):
    LibelleTransaction = apps.get_model('administration', 'LibelleTransaction')
    db_alias = schema_editor.connection.alias
    LibelleTransaction.objects.using(db_alias).filter(
        libelle__in=[LIBELLE_TRANSFERT_RECU, LIBELLE_TRANSFERT_VERSE],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('administration', '0005_alter_transaction_created_at_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_libelles, unseed_libelles),
    ]
