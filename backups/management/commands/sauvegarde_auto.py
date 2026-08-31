"""
Commande : python manage.py sauvegarde_auto

Sauvegarde automatique quotidienne de la base de données — à brancher sur une
tâche planifiée (ex: onglet "Tasks" de PythonAnywhere) entre 0h et 2h.

- Toujours de type « Complète » et chiffrée (cohérent avec la contrainte de
  SauvegardeRestoreView : seule une sauvegarde « Complète » est restaurable).
- En cas d'échec, alerte par email les administrateurs (voir
  backups/services.py::envoyer_alerte_echec_sauvegarde) — un échec silencieux
  pourrait sinon passer inaperçu plusieurs jours.
- Ne conserve que les NB_SAUVEGARDES_CONSERVEES sauvegardes RÉUSSIES les plus
  récentes ; tout le reste (anciennes réussies + tentatives échouées, qui ne
  contiennent aucun fichier exploitable) est supprimé, fichier compris.
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.audit import AuditAction, log_audit

from ...models import Sauvegarde
from ...services import envoyer_alerte_echec_sauvegarde, executer_sauvegarde

NB_SAUVEGARDES_CONSERVEES = 4


class Command(BaseCommand):
    help = "Sauvegarde automatique de la base de données, avec purge des anciennes sauvegardes."

    def handle(self, *args, **options):
        sauvegarde = self._creer_sauvegarde()
        resultat = executer_sauvegarde(sauvegarde)

        if resultat.ok:
            log_audit(
                AuditAction.DB_BACKUP, f'Sauvegarde automatique « {sauvegarde.nom} »',
                table='Sauvegarde', record_id=sauvegarde.pk,
            )
            self.stdout.write(self.style.SUCCESS(f'Sauvegarde automatique « {sauvegarde.nom} » créée avec succès.'))
        else:
            self.stderr.write(self.style.ERROR(f'Échec de la sauvegarde automatique « {sauvegarde.nom} » : {resultat.erreur}'))
            envoyer_alerte_echec_sauvegarde(sauvegarde.nom, resultat.erreur)

        nb_supprimees = self._purger_anciennes_sauvegardes()
        if nb_supprimees:
            self.stdout.write(f'{nb_supprimees} ancienne(s) sauvegarde(s) supprimée(s).')

    def _creer_sauvegarde(self):
        nom = f'sauvegarde_auto_{timezone.now():%Y%m%d_%H%M%S}'
        return Sauvegarde.objects.create(
            nom=nom, type_sauvegarde='Complète', chiffre=True,
            cree_par='Tâche automatique', reussie=False,
        )

    def _purger_anciennes_sauvegardes(self):
        a_conserver = set(
            Sauvegarde.objects.filter(reussie=True)
            .order_by('-created_at')
            .values_list('pk', flat=True)[:NB_SAUVEGARDES_CONSERVEES]
        )
        a_supprimer = Sauvegarde.objects.exclude(pk__in=a_conserver)
        nb = a_supprimer.count()
        for sauvegarde in a_supprimer:
            if sauvegarde.chemin_fichier:
                chemin_absolu = os.path.join(settings.DB_BACKUPS_DIR, sauvegarde.chemin_fichier)
                if os.path.isfile(chemin_absolu):
                    os.remove(chemin_absolu)
            sauvegarde.delete()
        return nb
