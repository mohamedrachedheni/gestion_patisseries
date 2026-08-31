"""
Commande : python manage.py export_donnees_excel [--output CHEMIN]

Exporte les tables de référence (voir core/management/reference_tables.py)
dans un classeur Excel, une feuille par table, dans l'ordre où elles doivent
être rechargées.
"""
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font

from core.management.reference_tables import nom_feuille, tables_reference


class Command(BaseCommand):
    help = 'Exporte les tables de référence vers un classeur Excel (une feuille par table)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output', default=None,
            help='Chemin du fichier .xlsx à générer '
                 '(par défaut : db_backups/donnees_reference_<horodatage>.xlsx)',
        )

    def handle(self, *args, **options):
        chemin = options['output']
        if not chemin:
            horodatage = timezone.now().strftime('%Y%m%d_%H%M%S')
            chemin = str(settings.DB_BACKUPS_DIR / f'donnees_reference_{horodatage}.xlsx')

        wb = Workbook()
        wb.remove(wb.active)  # feuille par défaut vide, inutile

        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Export des tables de référence ==='))
        for model in tables_reference():
            champs = [f.attname for f in model._meta.concrete_fields]
            lignes = list(model.objects.order_by('pk').values(*champs))

            ws = wb.create_sheet(title=nom_feuille(model))
            ws.append(champs)
            for cell in ws[1]:
                cell.font = Font(bold=True)
            ws.freeze_panes = 'A2'
            for ligne in lignes:
                ws.append([self._valeur_excel(ligne[champ]) for champ in champs])

            self.stdout.write(f'  • {model._meta.db_table} → {len(lignes)} lignes')

        wb.save(chemin)
        self.stdout.write(self.style.SUCCESS(f'\nExport terminé : {chemin}\n'))

    @staticmethod
    def _valeur_excel(valeur):
        """Decimal non géré nativement par openpyxl : conversion en float
        (montants à 3 décimales maximum, aucune perte de précision pour cet
        usage)."""
        if isinstance(valeur, Decimal):
            return float(valeur)
        return valeur
